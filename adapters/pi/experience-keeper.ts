/**
 * experience-keeper — pi 扩展适配器。
 *
 * 为什么需要这一层：pi 的会话断言不了「哪一轮刚结束」，扩展事件才是唯一可靠挂载点。
 * 本扩展只把事件归一化成统一契约 JSON 交给 Python 脚本，不读转录、不判定信号、
 * 不写候选；转录路径由 pi 自己管理的会话文件给出，压缩次数由本扩展按事件累加。
 *
 * 触发点：`turn_end` 落一轮采集，`session_compact` 累加压缩计数，
 * `session_shutdown` 清空计数（扩展实例在会话替换后会被重新绑定）。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

/** hook 脚本绝对路径，默认取全局 skill 目录；换机器或换位置时用 EXPERIENCE_HOOK_SCRIPT 覆盖。 */
const HOOK_SCRIPT =
  process.env.EXPERIENCE_HOOK_SCRIPT ??
  join(homedir(), ".agents", "skills", "experience-keeper", "scripts", "experience_hook.py");

/** 解释器默认按平台取：POSIX 上常见的是 python3，很多发行版没有 python 命令；特殊环境用 EXPERIENCE_PYTHON 覆盖。 */
const PYTHON_BIN =
  process.env.EXPERIENCE_PYTHON ?? (process.platform === "win32" ? "python" : "python3");

/** 固定 5 秒超时：采集超时或失败都静默，绝不拖住 pi 的轮次结束。 */
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

/** 统一契约的字段集合，三个适配器共用同一形状。 */
type HookPayload = {
  client: string;
  event: string;
  session_id: string;
  turn_id: string;
  cwd: string;
  transcript_path: string;
  compaction_count: number;
};

/** 把统一契约 JSON 交给 Python 脚本；任何失败都吞掉，采集不得影响客户端。 */
function runHook(payload: HookPayload): void {
  const env: Record<string, string> = {};
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

export default function experienceKeeper(pi: ExtensionAPI): void {
  /** 本会话已发生的压缩次数，取值规则见设计文档「采集层 / 五端适配」。 */
  let compactionCount = 0;

  pi.on("session_compact", async () => {
    compactionCount += 1;
  });

  pi.on("turn_end", async (event, ctx) => {
    try {
      const sessionFile = ctx.sessionManager.getSessionFile() ?? "";
      runHook({
        client: "pi",
        event: "turn_end",
        session_id: String(ctx.sessionManager.getSessionId()),
        // pi 的 turnIndex 从 0 起算，Python 侧按第 1 轮为 1 定位，这里换算一次。
        turn_id: String(event.turnIndex + 1),
        cwd: ctx.cwd,
        transcript_path: sessionFile,
        compaction_count: compactionCount,
      });
    } catch {
      // 扩展的异常一律不外抛：采集失败不能让 pi 的轮次失败。
    }
  });

  pi.on("session_shutdown", async () => {
    // 会话被替换或退出后扩展实例会重新绑定，计数必须从 0 起算，否则新会话会继承旧值。
    compactionCount = 0;
  });
}
