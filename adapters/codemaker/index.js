/**
 * experience-keeper — codemaker（OpenCode 体系）采集适配器。
 *
 * 为什么需要这一层：codemaker 的一轮结束在插件侧只表现为 `session.idle` 总线事件，
 * Python 拿不到「轮次边界」；这里把该事件归一化成统一契约 JSON 交给同一个 hook 脚本。
 *
 * 边界：只做事件名与字段的搬运，不做经验预筛表判定、脱敏、候选写入。纯 JavaScript（ESM），
 * 无构建步骤：OpenCode 插件入口直接由 codemaker 运行时按 ESM 加载。
 */

import { spawnSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/** hook 脚本绝对路径，默认取全局 skill 目录；换机器或换位置时用 EXPERIENCE_HOOK_SCRIPT 覆盖。 */
const HOOK_SCRIPT =
  process.env.EXPERIENCE_HOOK_SCRIPT ??
  join(homedir(), ".agents", "skills", "experience-keeper", "scripts", "experience_hook.py");

/** 解释器按需求固定为 `python`；Windows 与 POSIX 同名，特殊环境用 EXPERIENCE_PYTHON 覆盖。 */
const PYTHON_BIN = process.env.EXPERIENCE_PYTHON ?? "python";

/** 固定 5 秒超时：采集超时或失败都静默，绝不拖住 codemaker 的轮次结束。 */
const HOOK_TIMEOUT_MS = 5000;

/** 传给 hook 子进程的环境变量白名单：够 Python 启动与读中文，且不泄漏无关凭据变量。 */
const ENV_KEYS = [
  "PATH",
  "PATHEXT",
  "HOME",
  "USERPROFILE",
  "SystemRoot",
  "SystemDrive",
  "TEMP",
  "TMP",
  "LANG",
  "LC_ALL",
  "EXPERIENCE_HOME",
  "EXPERIENCE_PYTHON",
  "EXPERIENCE_HOOK_SCRIPT",
];

/** 会话存储根目录，与设计文档「五端适配」表里 codemaker 的转录来源一致。 */
const SESSION_STORE_ROOT = join(homedir(), ".codemaker", "session-store", "v1");

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.length > 0) {
      return value;
    }
  }
  return "";
}

/** 按会话 id 定位会话 JSON：一级哈希目录下直接放或放在 sessions/ 子目录两种布局都认。 */
function resolveTranscript(sessionID) {
  if (!sessionID) {
    return "";
  }
  try {
    for (const entry of readdirSync(SESSION_STORE_ROOT, { withFileTypes: true })) {
      if (!entry.isDirectory()) {
        continue;
      }
      const candidates = [
        join(SESSION_STORE_ROOT, entry.name, "sessions", `${sessionID}.json`),
        join(SESSION_STORE_ROOT, entry.name, `${sessionID}.json`),
      ];
      for (const candidate of candidates) {
        if (existsSync(candidate)) {
          return candidate;
        }
      }
    }
  } catch {
    // 目录不存在或不可读：留空字符串，由 Python 侧决定是否再找一次。
  }
  return "";
}

/** 把统一契约 JSON 交给 Python 脚本；任何失败都吞掉，采集不得影响客户端。 */
function runHook(payload) {
  const env = {};
  for (const key of ENV_KEYS) {
    const value = process.env[key];
    if (value !== undefined && value.length > 0) {
      env[key] = value;
    }
  }
  // 显式钉住 UTF-8：Windows 上 Python 默认按 ANSI 代码页解码 stdin，中文原文会变乱码。
  env.PYTHONIOENCODING = "utf-8";
  try {
    spawnSync(PYTHON_BIN, [HOOK_SCRIPT], {
      input: JSON.stringify(payload),
      encoding: "utf8",
      timeout: HOOK_TIMEOUT_MS,
      windowsHide: true,
      env,
    });
  } catch {
    // 脚本缺失、超时、非零退出都不处理：候选缺失是采集问题，不是会话问题。
  }
}

/**
 * 插件主体：codemaker 用 `{ id, server }` 作为插件模块形状，server 返回 hooks 对象。
 */
export const server = async (input) => {
  const fallbackCwd = firstString(input?.directory, input?.worktree, process.cwd());
  /** sessionID -> { turns, cwd, compaction }；turns 兼作 turn_id 的来源。 */
  const sessions = new Map();

  const stateOf = (sessionID, cwd) => {
    let state = sessions.get(sessionID);
    if (state === undefined) {
      state = { turns: 0, cwd: firstString(cwd), compaction: 0 };
      sessions.set(sessionID, state);
    }
    return state;
  };

  return {
    // OpenCode 的 event hook 收到的是 `{ event }` 包装；兼容直接给事件体的老形状。
    event: async (input) => {
      try {
        const event = isRecord(input?.event) ? input.event : input;
        const type = firstString(event?.type);
        const properties = isRecord(event?.properties) ? event.properties : {};

        if (type === "session.created") {
          const info = isRecord(properties.info) ? properties.info : {};
          const created = firstString(info.id);
          if (created) {
            // 会话自带工作目录，比插件进程的目录更贴近本轮实际所在项目。
            const state = stateOf(created, fallbackCwd);
            state.cwd = firstString(info.directory, state.cwd, fallbackCwd);
          }
          return;
        }

        const sessionID = firstString(properties.sessionID, properties.info?.id);
        if (!sessionID) {
          return;
        }

        if (type === "session.compacted") {
          // 压缩是位置标记，只影响复盘分段阅读，不参与任何判定。
          stateOf(sessionID, fallbackCwd).compaction += 1;
          return;
        }

        if (type !== "session.idle") {
          return;
        }

        // session.idle 是最接近「一轮结束」的信号：此时本轮消息已全部落进会话 JSON。
        const state = stateOf(sessionID, fallbackCwd);
        state.turns += 1;
        runHook({
          client: "codemaker",
          event: "turn_end",
          session_id: sessionID,
          // 进程内观察到的第几次 idle；codemaker 重启后接续的旧会话会重新从 1 起算。
          turn_id: String(state.turns),
          cwd: state.cwd || fallbackCwd,
          transcript_path: resolveTranscript(sessionID),
          compaction_count: state.compaction,
        });
      } catch {
        // 采集侧的异常一律不外抛：插件的 bug 不能让会话轮次失败。
      }
    },
  };
};

export default { id: "experience-keeper", server };
