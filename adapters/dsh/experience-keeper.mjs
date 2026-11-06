/**
 * experience-keeper — dsh 采集适配器（cordis 插件）。
 *
 * 为什么需要这一层：dsh 的会话日志由持久化后端写成
 * `~/.dsh/sessions/<projectKey>/<sessionId>/session.vN.jsonl.zstd`，路径拼接规则与
 * zstd 压缩都属于持久化后端的内部实现，Session 对象上不暴露任何文件路径。Python 侧
 * 因此拿不到本轮的原文证据。插件改为把观察到的 session 事件按轮归一化成一份纯文本
 * JSONL，把该文件路径当 `transcript_path` 交给 hook 脚本。
 *
 * 边界：只做事件名与字段的搬运，不做经验预筛表判定、脱敏、候选写入；不向会话上下文注入
 * 任何消息。归一化后的事件名与 `scripts/transcripts.py` 的 `parse_dsh` 一一对应。
 */

import { spawnSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

import { snapshotSessionEvents } from './session-events.mjs'

/** Cordis 插件名，供 loader 诊断使用。 */
export const name = 'experience-keeper'

/** hook 脚本绝对路径，默认取全局 skill 目录；换机器或换位置时用 EXPERIENCE_HOOK_SCRIPT 覆盖。 */
const HOOK_SCRIPT = process.env.EXPERIENCE_HOOK_SCRIPT
  ?? join(homedir(), '.agents', 'skills', 'experience-keeper', 'scripts', 'experience_hook.py')

/** 解释器默认按平台取：POSIX 上常见的是 python3，很多发行版没有 python 命令；特殊环境用 EXPERIENCE_PYTHON 覆盖。 */
const PYTHON_BIN =
  process.env.EXPERIENCE_PYTHON ?? (process.platform === 'win32' ? 'python' : 'python3')

/** 固定 5 秒超时：采集超时或失败都静默，绝不拖住客户端轮次结束。 */
const HOOK_TIMEOUT_MS = 5000

/** 传给 hook 子进程的环境变量白名单：够 Python 启动与读中文，且不泄漏无关凭据变量。 */
const ENV_KEYS = [
  'PATH', 'PATHEXT', 'HOME', 'USERPROFILE', 'SystemRoot', 'SystemDrive', 'TEMP', 'TMP',
  'LANG', 'LC_ALL', 'EXPERIENCE_HOME', 'EXPERIENCE_PYTHON', 'EXPERIENCE_HOOK_SCRIPT',
]

/** 归一化事件流的落盘目录，与 Python 侧 `store_home()` 的 EXPERIENCE_HOME 覆盖保持一致。 */
function transcriptDir() {
  const override = process.env.EXPERIENCE_HOME
  const base = override !== undefined && override.length > 0
    ? override
    : join(homedir(), '.agents', 'experience')
  return join(base, 'transcripts', 'dsh')
}

/** 只取文本块拼正文：图片、文件引用等块不是经验证据，搬过去只会灌大候选。 */
function textOf(content) {
  if (!Array.isArray(content)) return ''
  const parts = []
  for (const block of content) {
    if (block !== null && typeof block === 'object' && block.type === 'text' && typeof block.text === 'string') {
      parts.push(block.text)
    }
  }
  return parts.join('\n')
}

/** 工具参数在 dsh 日志里是模型产出的原始 JSON 字符串，解析失败时保留原文而不是丢弃。 */
function parseArguments(raw) {
  if (typeof raw !== 'string' || raw.length === 0) return {}
  try {
    const parsed = JSON.parse(raw)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return { raw }
    return parsed
  } catch {
    return { raw }
  }
}

/** 一条 tool/result 归一化为解析器认识的形状。
 *
 * dsh 0.2 的 `tool/result` 事件形状（与 `dsh-acp` 的 `toolResultUpdate` 一致）：
 * `event.data.message` 是 ToolResultMessage，`isError` 在 **message 级**，`content` 是
 * 文本内容块数组（每块形如 {"type": "text", "text": ...}）；失败原因另在 `event.data.error`。
 * 块级 `isError` 与嵌套 `content` 是早期形态，一并容忍，避免版本回退时采集静默失效。
 */
export function toolResultRecord(data) {
  const message = data?.message
  const blocks = Array.isArray(message?.content) ? message.content : []
  const first = blocks[0]
  const failed = message?.isError === true || first?.isError === true || data?.error !== undefined
  const text = textOf(blocks) || textOf(first?.content)
  return {
    type: 'tool_result',
    is_error: failed,
    // output 是判据取词面的现场：实测数值、状态旧值、幂等差异都从这里取，成功时也要留。
    // 解析器按 RESULT_CHARS 截断后落进候选，这里不截断，避免两侧口径不一致。
    output: text,
    error: failed ? text : '',
  }
}

/** 事件信封上的轮次号；turn/start 与 turn/end 都带，缺失时返回 undefined。 */
function turnOf(event) {
  const turn = event?.data?.turn
  return typeof turn === 'number' ? turn : undefined
}

/** 冷启动或 resume 时先扫一遍耐久历史，压缩次数才不会从 0 重新数。 */
function countCompactions(session) {
  try {
    let count = 0
    for (const event of snapshotSessionEvents(session)) {
      if (event.type === 'compaction/end') count += 1
    }
    return count
  } catch {
    // 历史不可读只是少一个位置标记，不影响本轮证据。
    return 0
  }
}

/** 落盘归一化事件流：先走 ctx.fs 的主机文件接口，被沙箱策略或后端拒绝时直写。 */
async function writeTranscript(ctx, target, content) {
  try {
    const fs = ctx.get('fs')
    if (fs !== undefined) {
      const resolved = await fs.resolve(target, { cwd: process.cwd() })
      await fs.writeText(resolved, content)
      return true
    }
  } catch {
    // 文件接口按模型可控路径设防；这里的路径由插件自己算出，退回 node:fs 直写。
  }
  try {
    mkdirSync(dirname(target), { recursive: true })
    writeFileSync(target, content, { encoding: 'utf8' })
    return true
  } catch {
    return false
  }
}

/** 把统一契约 JSON 交给 Python 脚本；任何失败都吞掉，采集不得影响客户端。 */
function runHook(payload) {
  const env = {}
  for (const key of ENV_KEYS) {
    const value = process.env[key]
    if (value !== undefined && value.length > 0) env[key] = value
  }
  // 显式钉住 UTF-8：Windows 上 Python 默认按 ANSI 代码页解码 stdin，中文原文会变乱码。
  env.PYTHONIOENCODING = 'utf-8'
  try {
    spawnSync(PYTHON_BIN, [HOOK_SCRIPT], {
      input: JSON.stringify(payload),
      encoding: 'utf8',
      timeout: HOOK_TIMEOUT_MS,
      windowsHide: true,
      env,
    })
  } catch {
    // 脚本缺失、超时、非零退出都不处理：候选缺失是采集问题，不是会话问题。
  }
}

/** 注册只观察、不注入的采集插件。 */
export function apply(ctx, config) {
  void config
  /** sessionId -> { compaction, turn, lines }；lines 是当前轮待落盘的归一化事件。 */
  const sessions = new Map()

  const stateOf = (session) => {
    const id = String(session.id ?? '')
    let state = sessions.get(id)
    if (state === undefined) {
      state = { compaction: countCompactions(session), turn: undefined, lines: [] }
      sessions.set(id, state)
    }
    return state
  }

  const openTurn = (state, turn) => {
    if (turn === undefined || state.turn === turn) return
    state.turn = turn
    // 压缩是位置标记：先把本轮之前已发生的次数写成标记行，落盘计数才与 payload 一致。
    state.lines = Array.from({ length: state.compaction }, () => ({ type: 'compaction' }))
  }

  const finishTurn = async (session, state, turn) => {
    if (state.turn !== turn) openTurn(state, turn)
    const lines = state.lines
    state.lines = []
    state.turn = undefined
    if (lines.length === 0) return

    const sessionId = String(session.id ?? '')
    const target = join(transcriptDir(), `${sessionId}-t${turn}.jsonl`)
    const content = `${lines.map((line) => JSON.stringify(line)).join('\n')}\n`
    const written = await writeTranscript(ctx, target, content)
    runHook({
      client: 'dsh',
      event: 'turn_end',
      session_id: sessionId,
      turn_id: String(turn),
      cwd: session.header?.cwd ?? process.cwd(),
      transcript_path: written ? target : '',
      compaction_count: state.compaction,
    })
  }

  ctx.on('session/event', async (session, event) => {
    const state = stateOf(session)
    try {
      switch (event.type) {
        case 'turn/start':
          openTurn(state, turnOf(event))
          break
        case 'user/message':
          state.lines.push({ type: 'user_message', text: textOf(event.data?.content) })
          break
        case 'assistant/message':
          state.lines.push({ type: 'agent_message', text: textOf(event.data?.message?.content) })
          break
        case 'tool/call':
          state.lines.push({
            type: 'tool_call',
            name: typeof event.data?.name === 'string' ? event.data.name : 'unknown',
            args: parseArguments(event.data?.arguments),
          })
          break
        case 'tool/result':
          state.lines.push(toolResultRecord(event.data))
          break
        case 'compaction/end':
          state.compaction += 1
          state.lines.push({ type: 'compaction' })
          break
        case 'turn/end': {
          const turn = turnOf(event) ?? state.turn
          if (turn === undefined) break
          // 只有 completed 是正常收尾，其余（aborted、error、blocked、interrupted、max-tokens）都记为中断。
          if (event.data?.reason?.kind !== 'completed') state.lines.push({ type: 'turn_aborted' })
          await finishTurn(session, state, turn)
          break
        }
        default:
          break
      }
    } catch {
      // 采集侧的异常一律不外抛：插件的 bug 不能让会话轮次失败。
      state.lines = []
      state.turn = undefined
    }
  })
}
