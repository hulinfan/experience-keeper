#!/usr/bin/env python3
"""经验预筛表实现：信号与人类纠正判定。

采集只对命中信号的轮次落候选，避免全量写入把经验库淹掉。
判定全部基于本轮转录事实，不做任何模型调用。

本文件是 SKILL.md 所指「经验预筛表」的执行定义，表定义见
docs/experience-prescreen-table-design.md（核心设计，唯一权威）。改信号必须同步该表、schema 枚举与测试。
"""

from __future__ import annotations

import json
import re

import experience_lib as lib

RETRY_THRESHOLD = 2
MULTI_SOURCE_THRESHOLD = 2
# 结论锚定源码行号的引用阈值：达到这个数说明本轮产出了文档与源码都查不到的对照结论。
FILE_REF_THRESHOLD = 3

# 显式否定与纠错表达。刻意不含「别」「停」这类单字：它们会误伤
# 「别用缓存」这种方案否决，也会命中「停机」「停用」等正常词。
DENIAL_MARKERS = (
    "不对",
    "不是",
    "错了",
    "搞错",
    "重来",
    "我说的是",
    "我不是说",
    "你理解错",
    "不需要",
    "不用",
    "不要",
    "不应该",
)
# 否定 agent 已提出的方案或结论。
REJECTION_MARKERS = ("不行", "不好", "换个", "换一种", "重新", "还是用", "改成", "别用", "不要用", "没必要")
# 用户直接给出正确结论或参数。
ANSWER_MARKERS = ("应该", "要用", "得用", "必须", "记得", "注意", "正确的是", "实际是")
# 用户自己动手完成了本该由 agent 完成的操作。
TAKEOVER_RE = re.compile(r"我(?:已经|自己|手动|直接)?(?:改|加|删|写|调|做|换)(?:好|完|了)")
# 用户重述诉求时的常见措辞。
RESTATE_MARKERS = ("再强调", "重申", "我的意思是", "我要的是", "重点是")
# 纯续跑口令：用户只是让 agent 接着做，不是对上一轮做法的反应。
CONTINUATION_MARKERS = ("继续", "接着", "往下", "goon", "continue", "carryon")
# 反应式纠正都短：贴进来的长文档与指令模板里满是「不要」「改成」，据此判纠正会产出垃圾候选。
CORRECTION_MAX_CHARS = 500
# 需求重述要求两条消息长度可比，否则短消息的字符二元组被长文包含会算出 1.0 的假相似度。
CORRECTION_MIN_LENGTH_RATIO = 0.5

# 跨 skill 编排只认「调用了别的 skill 的资产」。读 SKILL.md 是查文档不是编排：
# 用 rg / Get-Content 翻几个 skill 说明，长会话里遍地都是，认了等于每轮都命中。
# experience-keeper 是每会话固定入口（开场跑 prompt），必须排除。
SKILL_ASSET_RE = re.compile(r"skills[/\\]([A-Za-z0-9_\-]+)[/\\]scripts[/\\]")
SELF_SKILL_NAMES = ("experience-keeper",)
# 结论锚定源码行号：`src/core/action.c:1876` 这类引用说明结论来自实读代码，
# 不是文档与源码里现成可查的转述。
FILE_REF_RE = re.compile(
    r"[A-Za-z0-9_./\\-]+\.(?:c|h|cc|cpp|py|json|yaml|yml|md|go|ts|tsx|js|mjs|sh|rs|java|sql|tf|conf|ini):\d+"
)
CONCRETE_VALUE_RE = re.compile(r"(--?[a-zA-Z][\w\-]*|[A-Za-z]:\\|/[\w./\-]{3,}|\d{2,}|\w+\.\w{2,4}\b)")

# 会写出可复用资产的工具名。不同端的编辑工具名不同，覆盖到 write / edit / apply_patch 三种。
WRITE_TOOLS = ("write", "edit", "apply_patch")
# 叙述承载类工具：参数是 agent 写给子代理或用户的正文，不是本轮实测的现场。
# 把 prompt / message / question 正文当现场证据，方案讨论里的「不能引入快照方案」
# 会命中 blast_radius_guard，「删除三个对象 + 统计口径」会命中 cost_trap——
# 纯设计讨论轮由此落候选。这些工具的参数一律不进判据取词面。
NARRATIVE_TOOLS = frozenset(
    {
        "agent",
        "task",
        "sendmessage",
        "send_message",
        "send-message",
        "askuserquestion",
        "ask_user_question",
        "ask-user-question",
        "todowrite",
        "todo_write",
        "todo-write",
        "notebookedit",
        "notebook_edit",
        "sendfeedback",
        "send_feedback",
        "listagents",
        "list_agents",
        "message",
        "reply",
    }
)
# 委托类工具：写盘由子会话完成，父轮看不到写盘调用；参数里点名载体路径时按载体处理。
DELEGATE_TOOLS = ("agent", "task", "sendmessage", "send_message", "send-message")
WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"']+")
POSIX_PATH_RE = re.compile(r"(?<![\w.])/[\w./\-]{3,}\.\w{1,8}")
SCRIPT_SUFFIX_RE = re.compile(
    r"\.(?:sh|bash|zsh|ps1|bat|cmd|py|js|mjs|ts|go|rs|java|sql|tf|yaml|yml|json|j2|tmpl|conf|ini)\b",
    re.I,
)
DIAGRAM_MARKERS = (
    "```mermaid",
    "flowchart ",
    "sequencediagram",
    "graph td",
    "graph lr",
    "classdiagram",
    "statediagram",
    "erDiagram",
    "拓扑图",
    "流量路径",
)

# 依赖安装与版本同步：装完或同步完才能继续，是环境前置条件。
INSTALL_COMMAND_RE = re.compile(
    r"\b(?:pip3?|pipx|uv|poetry|conda|npm|pnpm|yarn|gem|cargo|go|apt|apt-get|yum|dnf|apk|brew|choco|winget|scoop)"
    r"\s+(?:install|add|sync|get|update|upgrade)\b",
    re.I,
)
INSTALL_TOOL_RE = re.compile(r"package|dependen|requirements|toolchain|install|npm|pip|conda|cargo|apt|brew", re.I)
# 带版本的依赖名：pyyaml==6.0.1、requests==2.31.0、left-pad@1.3.0。
SPEC_PIN_RE = re.compile(r"([A-Za-z][\w.\-]{1,40})\s*(?:==|@)(\d[\w.\-]*)")

# 环境复现与启动：跑通一次就值得记下来，下次不必重摸。
SETUP_COMMAND_RE = re.compile(
    r"\b(?:docker|podman|docker-compose)\s+(?:run|compose\s+up|up)\b"
    r"|\b(?:systemctl|service)\s+(?:start|restart|enable)\b"
    r"|\b(?:npm|pnpm|yarn)\s+(?:run\s+dev|run\s+build|ci|run\s+start)\b"
    r"|\bkubectl\s+(?:apply|create|port-forward)\b"
    r"|\bip\s+link\s+set\s+\w+\s+up\b"
    r"|\bip\s+addr\s+add\b"
    r"|\bmodprobe\b"
    r"|\bmount\s",
    re.I,
)
SETUP_TOOL_RE = re.compile(r"docker|compose|systemctl|service|kubectl|helm|ansible|terraform|modprobe", re.I)
SETUP_HINT_RE = re.compile(r"环境|部署|启动|起服务|本地跑|连通|复现", re.I)

# 先兜底后变更：快照、备份、灰度、限速、dry-run 都是踩过坑才加的防线。
BLAST_GUARD_KEYWORDS = (
    "--dry-run",
    "--dryrun",
    "dry_run",
    "dry-run",
    "--plan",
    "terraform plan",
    "terraform-plan",
    "--canary",
    "canary",
    "灰度",
    "回滚",
    "rollback",
    "备份",
    "backup",
    "快照",
    "snapshot",
    "限速",
    "rate-limit",
    "ratelimit",
    "throttle",
    "先预演",
    "试运行",
    "小流量",
)
# 动变更前留现场：抓包与状态快照，事后才能回溯。
EVIDENCE_CAPTURE_RE = re.compile(
    r"tcpdump\b|tshark\b|\bconntrack\s+-L\b|-j\s+LOG\b|iptables-save|nft\s+list|"
    r"pcap\b|flowlog|flow-log|抓包|导出\s*日志|保存\s*现场|配置快照|配置备份|基线",
    re.I,
)
# 不可逆或高危动作：只认工具参数里的动作本身。
# 环境名词（生产环境、线上、灰度环境、回滚、临时环境…）不进这张表：
# 它们出现在用户叙述与总结文档里遍地都是，命中的是「这轮聊到生产」而不是「这轮动了生产」。
HAZARD_ACTION_KEYWORDS = (
    "drop database",
    "删库",
    "清库",
    "truncate table",
    "mkfs",
    "wipefs",
    "dd if=",
    "iptables -F",
    "nft flush",
    "ip route flush",
    "sysctl -w",
)

# 命令族：诊断、变更、探测三类分开计数，供资生产生、批量变更与探测纪律判定。
DIAGNOSTIC_FAMILY_RE = re.compile(
    r"\b(?:tcpdump|tshark|conntrack|ip|iptables|nft|mtr|traceroute|tracepath|dig|nslookup|ss|netstat|sysctl|"
    r"kubectl|helm|aws|aliyun|az|gcloud|hcloud|curl|ping|bpftrace|perf|sar|iostat)\b",
    re.I,
)
# 变更动作词。是否算「真的在改配置」由 mutation_action() 判：re.I 下负向前瞻会把 camelCase
# 动作名（ModifyRouteEntry）一并排除，所以「独立成词」写在函数里，不写正则的后置断言。
MUTATION_FAMILY_RE = re.compile(
    r"(?:apply|create|delete|update|modify|set|patch|add|remove|attach|detach|"
    r"enable|disable|restart|stop|start|flush|reload|sync)",
    re.I,
)


def mutation_action(text: str) -> bool:
    """文本里是否出现真正的变更动作。

    判定要看词的上下文，三种情况都不算动作：
    - 左侧是标识符字符：函数名里的 `add`（`core_vpc_add`）；
    - 右侧是小写或数字续写：`address`、`added`、`settings`、`started` 是词内片段；
    - 右侧是大写但后面不是小写：`ADDRESS_MAP_SIZE`、`SETUP_TIMEOUT` 是全大写标识符。

    右侧「大写 + 紧跟小写」才算 camelCase 动作名（`DeleteRouteEntry`、`ModifyInstanceAttribute`）——
    云端 CLI 的动作名都长这样，全大写拼写的动作名不存在，所以这条既能保 camelCase 又能挡标识符。
    """
    for match in MUTATION_FAMILY_RE.finditer(text):
        start, end = match.span()
        word = match.group(0)
        before = text[start - 1] if start else ""
        if before.isascii() and (before.isalnum() or before == "_"):
            continue
        after = text[end] if end < len(text) else ""
        if after == "_" or (after.isascii() and (after.isdigit() or after.islower())):
            continue
        if after.isascii() and after.isupper():
            following = text[end + 1] if end + 1 < len(text) else ""
            if not (following.isascii() and following.islower()):
                continue
        elif not after and word.isupper():
            # 末尾没有后续字符时无法靠续写判断，全大写的匹配按标识符处理。
            continue
        return True
    return False
# 探测与采样：抓包、连通性探测、状态观测都算，单次是日常，连续多点是纪律。
PROBE_FAMILY_RE = re.compile(
    r"\b(?:tcpdump|tshark|ping|mtr|traceroute|tracepath|curl|dig|nslookup|nc|telnet|hping3|socat|"
    r"bpftrace|perf|conntrack|ss|netstat|ip|iptables|nft)\b",
    re.I,
)
# 探测要成组出现才算纪律：单次 ping 是日常，连续多点是采样。
PROBE_THRESHOLD = 3
DIAGNOSTIC_THRESHOLD = 3
BATCH_MUTATION_THRESHOLD = 3
CONVERGENCE_ATTEMPT_THRESHOLD = 4
QA_THRESHOLD = 3
QA_SIMILARITY = 0.75
# 跨会话巡检阈值：L3 只在经验巡检里跑，不进钩子热路径。
INTENT_REPEAT_THRESHOLD = 3
ARTIFACT_CHURN_THRESHOLD = 2
# 云资源标识：批量变更按不同标识去重计数。
# 用前缀锚定而不是 \b：单字母前缀（i、s、v）在普通英文里太多，
# \b 会把 "s-connect" 这类词尾当成 sg- 资源，把普通命令误判成批量变更。
# 断言写成 (?![a-z0-9-]) 这种「不该出现的字符集」形式：
# re.I 会 casefold 否定前瞻里的字符类，写成 (?![A-Za-z0-9]) 会把 camelCase 后缀也排除掉。
RESOURCE_ID_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?:i|s|v|sg|subnet|eni|eip|nat|vsw|rtb|acl|clb|ngw|cgw|vpc|dns|slb)-[0-9a-z]{6,}(?![a-z0-9-])",
    re.I,
)


def resource_ids(text: str) -> set[str]:
    """抠出云资源标识，按小写去重。"""
    return {match.group(0).lower() for match in RESOURCE_ID_RE.finditer(text)}


# --------------------------------------------------------------------------
# 深水区判据：软硬件底座、观察者效应、协议边缘、控制面状态机。
# 这四类隐性知识流失后排障成本最高，判据必须窄，宁可漏不可错。
# --------------------------------------------------------------------------

# 卸载特性：同一份规则在不同网卡与固件上表现不同。
OFFLOAD_FEATURE_RE = re.compile(
    r"(?<![A-Za-z])(?:gro|lro|tso|gso|ufo|rxvlan|txvlan|rxchecksum|txchecksum|checksum|"
    r"scatter[-_]?gather|sg|rxhash|ntuple|tx[-_]?udp_tnl|hw[-_]?tc[-_]?offload|"
    r"generic[-_]?receive[-_]?offload|large[-_]?receive[-_]?offload|tcp[-_]?segmentation)(?![A-Za-z])",
    re.I,
)
OFFLOAD_CMD_RE = re.compile(r"ethtool\s+(?:-[kK]|--show-features|--features|--offload)", re.I)
# 部署底座：条目要能写成「版本/硬件矩阵」，这些词说明现场交代了底座。
PLATFORM_CONTEXT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:intel|mellanox|mlx[45]|broadcom|bnxt|bcm|i40e|ice|ixgbe|vfio|dpdk|"
    r"kernel|uname|固件|firmware|驱动|driver|网卡|nic|机型)(?![A-Za-z0-9])",
    re.I,
)
# 观察到异常数值：包长、巨帧、校验和错、分片。
OFFLOAD_ANOMALY_RE = re.compile(
    r"bad\s*checksum|checksum\s*(?:error|incorrect|wrong|失败|错误)|"
    r"jumbo|巨帧|mtu\s*(?:\d{4,}|mismatch|不一致)|"
    r"fragment|分片|truncated|截断|length\s*(?:error|mismatch)|包长",
    re.I,
)
# 观察者效应：高强度观测工具。
HEAVY_OBSERVER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:tcpdump|tshark|pdump|gwsvc-pdump|strace|perf|bpftrace|ftrace|"
    r"gdb|ltrace|blktrace|sysdig)(?![A-Za-z0-9])",
    re.I,
)
# 高负载语境：观测动作在高并发下才有风险。
HIGH_LOAD_RE = re.compile(
    r"\b\d{4,}\s*(?:cps|qps|pps|tps|rps)|百万|十万|高并发|百[gG]|100[kKgG]|现网|生产|线上",
    re.I,
)
# 退化症状：观测之后紧接着出现的性能或可用性问题。
DEGRADATION_RE = re.compile(
    r"(?:cpu|内核态|软中断|si)\s*(?:打满|飙|100|饱和|满载)|"
    r"(?:时延|延迟|rt|latency)\s*(?:激增|飙升|上涨|变高)|"
    r"(?:吞吐|tps|qps|pps)\s*(?:断崖|下跌|下降|掉)|"
    r"(?:丢包|drop)\s*(?:增加|上升|加剧)|"
    r"服务(?:重启|挂|不可用)|oom|重启|崩溃|"
    r"抓包.{0,12}(?:掉|降|影响)",
    re.I,
)
# 抓包范围不受限：没有包数或时长上限的抓包，才是危险动作。
UNBOUNDED_CAPTURE_RE = re.compile(
    r"(?:tcpdump|tshark|pdump)[^|;\n]*"
    r"(?:(?<!\S)-c\s*\d+|(?<!\S)-G\s*\d+|(?<!\S)-w\s*\S+|\bcount\s*[:=]\s*\d+)",
    re.I,
)
CAPTURE_CMD_RE = re.compile(r"(?:tcpdump|tshark|pdump|gwsvc-pdump)[^|;\n]*", re.I)

# 协议边缘特征：标准协议在云网络中间件里的非标表现。
PROTOCOL_EDGE_RE = re.compile(
    r"分片|fragment(?:ation|ed)?|mtu\s*(?:黑洞|blackhole|不一致|mismatch|发现失败|探测失败)|"
    r"pmtud|path\s*mtu|黑hole|黑洞路由|"
    r"非对称路由|asymmetric\s*rout|"
    r"rst\b|syn\s*(?:flood|retrans|重传)|"
    r"conntrack.{0,10}(?:无法建立|建不起来|失败|不建立)|"
    r"乱序|out[-_]of[-_]order|dup\s*ack|重复\s*ack|"
    r"half[-_]?open|半开|状态机异常|tcp\s*状态异常|"
    r"nat64|ds[-_]?lite",
    re.I,
)
# 落到参数或路由调整：协议边缘现象配这些动作，才算闭环。
PROTOCOL_FIX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sysctl|ip\s+route|ip\s+-6\s+route|iptables|nft|route\s+(?:add|del|change)|"
    r"mtu|mss|tcp_mtu_probing|nf_conntrack|rp_filter|forwarding|accept_ra|"
    r"改写|调整|下发)(?![A-Za-z0-9])",
    re.I,
)

# 控制面状态机：轮询不动、或状态冲突。
STATE_POLL_CMD_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:describe|get|show|status|query|list|inspect)",
    re.I,
)
# 轮询必须落在控制面 CLI 上：`Get-Content`、`workspace.py status` 这类文本命中
# 会把读文档、读代码的参数当成状态轮询。
STATE_POLL_CLI_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:aliyun|aws|az|gcloud|hcloud|kubectl|helm|etcdctl|consul|"
    r"systemctl|docker|nft|iptables|ovs-vsctl|gwsvc-ctl|ip)\b",
    re.I,
)
# 轮询同一目标到第 3 次状态仍不变：状态机卡住的现场。
STATE_POLL_THRESHOLD = 3
STATE_CONFLICT_RE = re.compile(
    r"resource\s*(?:is\s*)?(?:in\s*use|locked|busy)|state\s*conflict|conflict(?:ing)?\s*state|"
    r"(?:被|正在)占用|状态冲突|操作冲突|依赖未满足|前置依赖|"
    r"invalid\s*state|not\s*in\s*(?:an?\s*)?(?:active|available|stable)\s*state",
    re.I,
)
# 解决动作：改顺序、加等待、加重试、先解绑。
STATE_RESOLUTION_RE = re.compile(
    r"顺序|先后|依赖|等待|等\s*\d+\s*(?:秒|s\b|分钟)|重试|退避|间隔|"
    r"先(?:解绑|释放|删除|创建)|再(?:绑定|创建|下发)|after\s+(?:waiting|retry)",
    re.I,
)

# 状态读取：用于「变更后没收敛」的判定。
# 前缀用否定后顾而不是 \b：CLI 动作名是 camelCase（DescribeRouteEntry），
# \b 要求词尾边界，会把 Describe 整个漏掉。
# 否定断言必须写 [A-Za-z0-9]：re.I 下 [a-z0-9] 也忽略大小写，
# `Describe` 后面的大写 `I` 会被 (?![a-z0-9]) 命中，导致整条匹配失败。
STATUS_CALL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:describe|get|show|list|status|query|inspect|read)",
    re.I,
)
OLD_STATE_RE = re.compile(
    r"\b(?:pending|creating|updating|inactive|down|not\s+ready|old|previous|stale|unchanged)\b"
    r"|未生效|仍是|仍旧|没生效|还是旧|未更新|待生效",
    re.I,
)
QUERY_STATEMENT_RE = re.compile(
    r"\bselect\s+.+?\s+from\s+\w"
    r"|\bwhere\s+\w+\s*(?:=|like|in\b|between\b)"
    r"|\bgroup\s+by\s+\w|\border\s+by\s+\w"
    r"|\|\s*(?:stats|where|timechart|table)\b"
    r"|\bterms\s+agg\b|\bdate_histogram\b"
    r"|\bindex\s*[:=]\s*\S|\bes\s+query\b|\bpromql\b"
    r"|\b(?:rate|increase|histogram_quantile)\(",
    re.I,
)
# 推翻先前结论：必须是明确的翻案措辞，不能只靠「之前」「改为」这类日常用词。
REVERSAL_MARKERS = ("推翻", "推翻原", "不再适用", "不再成立", "重新评估", "原判断有误", "结论改了")

# 反直觉结论：与预期相反的因果，只能从人的措辞里认。
COUNTERINTUITIVE_MARKERS = (
    "居然",
    "竟然",
    "没想到",
    "想不到",
    "反而",
    "其实是",
    "原来不是",
    "原来问题在",
    "原来根因",
    "跟预期相反",
    "和预期相反",
    "意外的是",
    "出乎意料",
)
# 结论句式：现象与根因的映射落在这里。
CONCLUSION_MARKERS = (
    "根因是",
    "根因在于",
    "原因是",
    "真正的原因是",
    "问题出在",
    "定位到",
    "最终确认",
    "结论是",
)
# 错误原文里的前置条件失败点：权限、白名单、证书、代理、配额。
# proxy / kms 这类短词必须独立成词：分支名 `Mars/feature/gameproxy`、类名里的前缀
# 会命中子串，把一次代码阅读判成「踩过白名单或代理的坑」。
PREREQ_ERROR_RE = re.compile(
    r"\b403\b|\b401\b|\b407\b|forbidden|unauthorized|accessdenied|access\s+denied|permission\s+denied|"
    r"whitelist|white\s+list|白名单|not\s+authorized|(?<![A-Za-z0-9])kms(?![A-Za-z0-9])|sts:|"
    r"certificate|x509|self[- ]signed|证书|"
    r"(?<![A-Za-z0-9])proxy(?![A-Za-z0-9])|代理|"
    r"quota|throttl|rate\s*exceed|limit\s*exceed|too\s+many\s+requests|配额|限流|限速",
    re.I,
)
# 只在本机成立的数值与口径：单位必须有，裸数字不算。
VALUE_UNIT_RE = re.compile(
    r"\d[\d,\.]*\s*(?:ms|毫秒|s\b|秒|min|分钟|小时|h\b|pps|qps|tps|rps|gbps|mbps|kbps|gb|mb|kb|tb|"
    r"条|个|次|并发|连接|连接数|会话|表项|socket|fd|端口|seconds?|times)",
    re.I,
)
CONNECTION_CAPACITY_RE = re.compile(
    r"连接|会话|conntrack|表项|socket|fd|句柄|\bconn", re.I
)
# 本环境专属口径：量词 + 实测措辞的组合。
# 不能直接用数值正则：「3 次」「2 个」在中文闲聊里满地都是，会把整个 cost_trap 变成噪声源。
# 也不能认裸 \d{3,}：年份（2026）、提交号前缀（8586257899）、短哈希（764fd）都会被当成量词。
ENV_MEASURE_RE = re.compile(
    r"\d[\d,\.]*\s*(?:万|w)?\s*(?:ms|毫秒|秒|s\b|min|分钟|pps|qps|gbps|mbps|条|连接|会话|表项|cps)"
    r"|[一二三四五六七八九十百千两]+\s*万?\s*(?:条|个|次|连接|会话)",
    re.I,
)
# 年份与长数字不是量词：前者是日期，后者是哈希或 ID。
MEASURE_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
MEASURE_LONG_ID_RE = re.compile(r"\d{7,}")
ENV_MEASURE_HINTS = ("只在本机", "本环境", "实测", "这台机器", "本机", "阈值", "口径", "换算")
COST_DECISION_MARKERS = (
    "费用",
    "计费",
    "成本",
    "带宽费",
    "流量费",
    "跨区流量",
    "sla",
    "合规",
    "配额",
    "贵",
    "省钱",
)
# 取舍结构：判据词必须与「二选一」同句才算决策现场。
TAKEOFF_STRUCTURE_RE = re.compile(
    r"方案\s*[AB12甲乙]|方案\s*[一二三]|vs\.?\s|还是用|二选一|取舍|权衡|对比后|二种方案|两种方案|换方案",
    re.I,
)
# 决定判词：只描述「成本高」是现状陈述，做了选择才是决策。
DECISION_VERB_RE = re.compile(r"采用|选择|决定|取舍后|最终用|改成|换成|优先选|落地为", re.I)
NONFUNCTIONAL_FLOOR_RE = re.compile(
    r"nf_conntrack_max|nf_conntrack_buckets|nf_conntrack_tcp_timeout|ip_local_port_range|"
    r"somaxconn|tcp_max_syn_backlog|tcp_rmem|tcp_wmem|tcp_tw_reuse|overcommit_memory|"
    r"file-max|nofile|ulimit|netdev_max_backlog|rmem_max|wmem_max|swappiness",
    re.I,
)
# 数值与单位：单位是判据的一部分，裸数字不算。
# 中文数值（五万条、10 万次）也要认，云网络的容量描述大量用「万」。
VALUE_UNIT_RE = re.compile(
    r"\d[\d,\.]*\s*(?:万|w)?\s*(?:ms|毫秒|s\b|秒|min|分钟|小时|h\b|pps|qps|tps|rps|gbps|mbps|kbps|gb|mb|kb|tb|"
    r"条|个|次|并发|连接|连接数|会话|表项|socket|fd|端口|seconds?|times|cps)"
    r"|[一二三四五六七八九十百千两]+\s*(?:万|w)?\s*(?:条|个|次|台|路|毫秒|秒|分钟)",
    re.I,
)
# 硬限语境下的大数字：`65535 上限`、`端口耗尽 65535` 都不带单位。
BARE_LARGE_NUMBER_RE = re.compile(r"\b\d{4,}\b|\b\d+\s*万\b")
# 性能量纲：数值带这些单位时，全句找性能语境即可，不必局限在数值附近。
PERFORMANCE_UNIT_RE = re.compile(r"\d[\d,\.]*\s*(?:pps|qps|tps|rps|cps|gbps|mbps|kbps)", re.I)
PERFORMANCE_MARKERS = (
    "丢包",
    "打满",
    "跑满",
    "饱和",
    "瓶颈",
    "达到",
    "压测",
    "极限",
    "上限",
    "限速",
    "最大",
    "limit",
    "max",
)
# 硬限制（平台或协议的天花板）：可转成监控告警规则。
HARD_LIMIT_MARKERS = (
    "上限",
    "耗尽",
    "打满",
    "用尽",
    "天花板",
    "最大",
    "limit",
    "max",
    "exhaust",
    "65535",
    "port exhaustion",
    "端口耗尽",
)
# 软限制（本环境的经验值）：可转成开发前置约束。
SOFT_LIMIT_MARKERS = (
    "超过",
    "大于",
    "会 oom",
    "会超时",
    "会变慢",
    "建议不超过",
    "经验值",
    "阈值",
    "实测",
    "单次查询",
    "只在本机",
    "本环境",
)
# 幂等对比的兜底：抽不出 `字段=值` 时，只有短小标量结果可比（`conntrack -C` 的一个计数）。
# 长输出（构建日志、测试日志）每次都有差异，拿它比等于把「跑了一次命令」记成幂等被破坏。
SCALAR_RESULT_CHARS = 200
SCALAR_RESULT_LINES = 3
# 幂等对比要忽略的动态字段：每次调用都在变，拿它们比对等于必然命中。
# 用子串包含判断而不是正则词尾：`time` 会吃掉 `timeout`，害得真字段被当成动态字段剔掉。
VOLATILE_FIELD_MARKERS = (
    "timestamp",
    "time",
    "date",
    "reqid",
    "requestid",
    "traceid",
    "spanid",
    "transactionid",
    "txnid",
    "uuid",
    "nonce",
    "token",
    "etag",
    "serial",
    "requestid",
    "elapsed",
    "duration",
    "latency",
    "createdat",
    "updatedat",
    "expire",
    "ttl",
)
# 字段赋值对：用于把返回体规范化后再比。
FIELD_ASSIGN_RE = re.compile(r"([A-Za-z_][\w\-]*)\s*[:=]\s*([^\s,;{}\[\]\"']+)")
# 诊断开关：加上它说明前一次「成功」不足以判断。
INTENSIFIER_RE = re.compile(
    r"(?<!\w)--(?:verbose|debug|trace|force)(?!\w)",
    re.I,
)
# 疑问或否定结论：没看到预期现象，就是静默失败的现场描述。
NEGATIVE_CONCLUSION_RE = re.compile(
    r"并未|没有看到|没看到|没生效|未生效|不生效|不符合预期|和预期不符|与预期不符|"
    r"为什么还是|怎么会|奇怪|异常的是|查不到|看不到|对不上",
    re.I,
)


def _text_of(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", text or "")
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def _similarity(left: str, right: str) -> float:
    """用字符二元组重合度衡量两句话是否在讲同一件事，适配中文。"""
    first, second = _bigrams(left), _bigrams(right)
    if not first or not second:
        return 0.0
    return len(first & second) / min(len(first), len(second))


def _tool_signature(tool: dict) -> str:
    return f"{tool.get('name', '')}::{_text_of(tool.get('args', ''))}"


def is_narrative_tool(tool: dict) -> bool:
    """工具参数是否是 agent 写的正文：是则只说明「在编排」，不能当现场证据。"""
    return str(tool.get("name") or "").strip().lower() in NARRATIVE_TOOLS


def _judgement_args(tool: dict) -> str:
    """判据取词面用的参数文本：叙述承载类工具返回空串，脚本文本不算命令行。"""
    if is_narrative_tool(tool):
        return ""
    return _command_text(tool)


# here-doc / here-string：参数里塞一整段脚本时，正文里的「回滚」「一次本机」是脚本内容，
# 不是本轮的兜底动作与实测数值。判定只看命令行，正文整段去掉。
HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_script_bodies(text: str) -> str:
    """去掉 here-doc 与 PowerShell here-string 的正文，保留其余命令行。"""
    kept: list[str] = []
    closing = ""
    here_string = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if closing:
            if stripped == closing:
                closing = ""
            continue
        if here_string:
            if stripped == here_string:
                here_string = ""
            continue
        marker = HEREDOC_START_RE.search(line)
        if marker:
            kept.append(line)
            closing = marker.group(2)
            continue
        if line.rstrip().endswith("@'"):
            kept.append(line)
            here_string = "'@"
            continue
        if line.rstrip().endswith('@"'):
            kept.append(line)
            here_string = '"@'
            continue
        kept.append(line)
    return "\n".join(kept)


def _command_text(tool: dict) -> str:
    """工具参数里的命令行：脚本正文不算。"""
    return strip_script_bodies(_args_text(tool.get("args", "")))


def _args_text(value) -> str:
    """把工具参数压成一段文本：dict / list / 字符串都能覆盖。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_args_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_args_text(item) for item in value)
    return "" if value is None else str(value)


def _arg_paths(value) -> list[str]:
    """抠出参数里的文件路径，供资产生成判定。

    递归取各字符串值本身，不走 JSON 序列化：Windows 路径在 JSON 文本里会被转义成
    双反斜杠，落进登记表就与实际路径对不上，指纹与存在性判断全废。
    """
    if isinstance(value, str):
        return WINDOWS_PATH_RE.findall(value) + POSIX_PATH_RE.findall(value)
    if isinstance(value, dict):
        return [path for item in value.values() for path in _arg_paths(item)]
    if isinstance(value, (list, tuple)):
        return [path for item in value for path in _arg_paths(item)]
    return []


def _flags(text: str, keywords) -> bool:
    return any(keyword in text for keyword in keywords)


# --------------------------------------------------------------------------
# 判据取词面：判据型信号只认现场证据，不认转述。
# 现场 = 工具参数 + 工具错误原文 + 非文档类工具结果；
# 转述 = 用户消息与 assistant 叙述、读 .md/.txt 得到的文档正文。
# 复盘中 100% 误报的 env_hazard / perf_limit / cost_decision / protocol_edge / cost_trap
# 全部来自后者：文档里的「50%」「分片」「配额」是描述，不是本轮实测到的判据。
# --------------------------------------------------------------------------
DOC_READ_RE = re.compile(r"\.(?:md|markdown|txt|rst|docx|xlsx|pdf)\b", re.I)
# 文档正文的形态特征：带 ≥2 个 Markdown 标题行的结果就是文档内容，
# 即使它是被 rg 或 skill 抓取回来的（参数里看不到 .md），同样属于转述。
DOC_BODY_RE = re.compile(r"(?m)^#{1,6} \S")
# 只读检索命令：参数里出现闭环动作词（搜 mtu、搜 sysctl）不等于真的落到调整。
READONLY_CMD_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:rg|grep|findstr|select-string|get-content|cat|head|tail|less|type|find|"
    r"test-path|get-childitem|get-item|resolve-path)\b",
    re.I,
)
# 仓库内容检索：输出是代码与历史的转述，不是本轮实测，不进判据取词面。
# 刻意不含 `cat`/`head`/`tail`/`Get-Content`：它们读的可能是 `/proc`、日志、抓包统计，那是现场。
CODE_SEARCH_RE = re.compile(r"(?<![A-Za-z0-9])(?:rg|grep|findstr|select-string|find)(?![A-Za-z0-9])", re.I)
READONLY_GIT_SUB = (
    "show",
    "log",
    "diff",
    "blame",
    "grep",
    "status",
    "branch",
    "rev-parse",
    "ls-tree",
    "ls-files",
    "cat-file",
    "describe",
    "rev-list",
    "shortlog",
    "show-ref",
    "for-each-ref",
)
# 只读的 git 调用：命令可能被塞在 `$c = git show ...` 这类赋值或管道中间，
# 只按首 token 认命令族会漏，因此再按子命令扫一遍；`git -C <路径> status` 的全局参数也要认。
READONLY_GIT_RE = re.compile(
    r"(?<![A-Za-z0-9])git(?:\.exe)?(?:\s+-[A-Za-z]\s+\S+|\s+--[\w\-]+=\S+)*\s+"
    r"(?:" + "|".join(re.escape(sub) for sub in READONLY_GIT_SUB) + r")\b",
    re.I,
)
# agent 工具链自身的调用：查经验库、看工作区状态。读的是本地工具状态，不是现场。
AGENT_TOOLING_RE = re.compile(
    r"[/\\]skills[/\\][\w.\-]+[/\\]scripts[/\\]|\.harness[/\\]scripts[/\\]|"
    r"(?<![A-Za-z0-9])(?:experience|workspace)\.py(?![A-Za-z0-9])",
    re.I,
)


def _reads_source_file(tool: dict) -> bool:
    """参数里的路径是否都是源码或文档：读它们属于「看代码查文档」。"""
    paths = _arg_paths(tool.get("args", ""))
    return bool(paths) and all(SOURCE_SUFFIX_RE.search(path) for path in paths)


def _judgement_result(tool: dict) -> str:
    """工具结果里算现场证据的部分。

    命令输出是现场（实测数值、状态旧值、抓包统计）；读文档、读源码、检索仓库拿回来的正文是转述，
    源码里的 `MAX_DESTS`、文档里的「压测到 5000 pps」都不是本轮实测到的东西。
    读 `/proc`、日志、抓包文件属于读现场，照旧进取词面——按路径判定，不按命令名一刀切。
    """
    if is_narrative_tool(tool):
        return ""
    result = str(tool.get("result") or "")
    if not result:
        return ""
    args_text = _args_text(tool.get("args", ""))
    if DOC_READ_RE.search(args_text) or len(DOC_BODY_RE.findall(result)) >= 2:
        return ""
    if _reads_source_file(tool):
        return ""
    if CODE_SEARCH_RE.search(args_text) or READONLY_GIT_RE.search(args_text):
        return ""
    return result


def _tool_evidence(tool: dict) -> str:
    """单个调用的现场证据：命令行 + 命令输出 + 错误原文（脚本文本不算命令行）。"""
    if is_narrative_tool(tool):
        return ""
    return f"{_command_text(tool)} {_judgement_result(tool)} {tool.get('error') or ''}".strip()


def _evidence_text(tools: list[dict]) -> str:
    return " ".join(_tool_evidence(tool) for tool in tools)


# shell 包装：各端把命令塞进 powershell -Command / sh -c，不去掉的话
# 所有命令的首 token 都是同一个解释器，命令族退化成一个桶。
SHELL_WRAPPER_RE = re.compile(r"-Command\s+", re.I)
SHELL_DASH_C_RE = re.compile(
    r"^\s*(?:cmd(?:\.exe)?\s+/c|(?:ba|z|k)?sh\s+-c|pwsh\s+-c|powershell\s+-c)\s+",
    re.I | re.S,
)
INTERPRETER_NAMES = ("python", "python3", "python.exe", "node", "node.exe", "perl", "ruby", "pwsh", "powershell", "bash", "sh")


def _strip_shell_wrapper(command: str) -> str:
    parts = SHELL_WRAPPER_RE.split(command, maxsplit=1)
    if len(parts) == 2:
        command = parts[1]
    match = SHELL_DASH_C_RE.match(command)
    if match:
        command = command[match.end() :]
    return command.lstrip("\"'").strip()


def _command_family(command: str) -> tuple[str, str]:
    """命令族：去掉 shell 包装后取首个有效 token 与子命令。

    解释器（python/node/bash…）取脚本路径或 `-c`/`-m`，否则取子命令：
    `git show` 与 `git log` 是两族，`aliyun cms QueryMetric` 与 `aliyun vpc DeleteRouteEntry` 也是。
    """
    tokens = [token.strip("\"'") for token in re.split(r"\s+", _strip_shell_wrapper(command)) if token.strip()]
    index = 0
    while index < len(tokens) and (tokens[index].startswith("-") or "=" in tokens[index]):
        index += 1
    if index >= len(tokens):
        return ("", "")
    first = tokens[index].lower()
    second = ""
    if first in INTERPRETER_NAMES:
        cursor = index + 1
        while cursor < len(tokens):
            token = tokens[cursor]
            if token in ("-c", "-m"):
                second = token
                break
            if token.startswith("-"):
                cursor += 1
                continue
            second = token
            break
    elif index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
        second = tokens[index + 1]
    return (first, second)


def _family_key(tool: dict) -> str:
    first, second = _command_family(_args_text(tool.get("args", "")))
    return f"{tool.get('name', '')}::{first}::{second}"


def _is_readonly_command(args_text: str) -> bool:
    """参数是不是「只读检索或 git 读取」：这类调用看的是代码与历史，不是现场。"""
    stripped = _strip_shell_wrapper(args_text)
    if READONLY_GIT_RE.search(stripped):
        return True
    first, second = _command_family(stripped)
    if first == "git" and second.lower() in READONLY_GIT_SUB:
        return True
    return bool(READONLY_CMD_RE.search(stripped))


def _change_args(tools: list[dict]) -> str:
    """变更类调用的参数：只读检索、git 查询与叙述承载类工具都不算「落到调整」。"""
    kept = []
    for tool in tools:
        if is_narrative_tool(tool):
            continue
        args_text = _command_text(tool)
        if _is_readonly_command(args_text):
            continue
        kept.append(args_text)
    return " ".join(kept)


# CLI 用法错误与测试红绿：都不是可复用的恢复策略。
# 「参数写错了」「测试先红后绿」是工具与开发循环的常态，读 --help 与跑测试就能复现。
USAGE_ERROR_RE = re.compile(
    r"unrecognized argument|unknown option|invalid option|usage:|unknown repository|"
    r"io error for operation|no such file|directory not found|command not found|"
    r"不是内部或外部命令|cannot find the path",
    re.I,
)
TEST_RESULT_RE = re.compile(r"\bFAIL\b|FAILED|AssertionError|assertion failed|tests? failed|\d+ failed", re.I)


def _reached_ok(tool: dict) -> bool:
    """调用到达成功态。部分解析器只给 called，与 ok 同为「没失败」。"""
    return str(tool.get("status") or "") in ("ok", "called")


# 一次性痕迹：现场值被写死在赋值语句里，换环境即失效。
# 只认赋值语境，避免把注释、示例、文档里的 IP 误判成不可复用。
ONE_OFF_ASSIGN_RE = re.compile(
    r"^[ \t]*(?!#)[\w.$\[\]'\"]+[ \t]*[:=][ \t]*['\"]?"
    r"(?:(?:\d{1,3}\.){3}\d{1,3}\b"
    r"|(?:i|s|v|sg|subnet|eni|eip|nat|vsw|rtb|acl|clb|ngw|cgw)-[0-9a-z]{6,}\b"
    r"|arn:aws:"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    r"|(?:AKIA|LTAI)[0-9A-Za-z]{10,}\b)",
    re.I | re.M,
)
# 复用能力：能被换环境重复调用。区分 argparse 与 getopt：后者只有一层可选参数，不足以支撑复用。
REUSE_PARAM_RE = re.compile(r"argparse|add_argument|click\.|typer\.|sys\.argv|os\.environ|getenv|process\.env", re.I)
# 写在模块顶层的具体值：临时脚本的典型形态，换环境要先改源码。
MODULE_LEVEL_RE = re.compile(r"^[\w$]+\s*[:=]", re.M)
# 结构化配置本身可复用：现场值由下发时替换模板变量，不是写死。
CONFIG_SUFFIX_RE = re.compile(r"\.(?:tf|yaml|yml|j2|tmpl|conf|ini|json)\b", re.I)
# 临时落点：本身就是一次性脚本的信号，脚本写完即弃。
TEMP_PATH_RE = re.compile(r"[/\\]tmp[/\\]|[/\\]temp[/\\]|%TEMP%|\$env:TEMP|/dev/shm|AppData[/\\]Local[/\\]Temp", re.I)

# Process signals can explain why a turn looked interesting, but they do not
# establish that it produced reusable knowledge.
# `idempotency_mismatch` 判据来自真实缺陷，但转正率只有 13%（4/31 判决）：绝大多数命中
# 是「同一命令跑两次结果不同」的过程现象，不是可复用知识，`push 各仓改动` 这类纯操作轮也
# 会命中。它保留为复盘线索，不再单独准入；要落候选必须与下述证据型信号同时出现。
REUSABLE_EVIDENCE_SIGNALS = frozenset({
    "asset_script",
    "asset_recipe",
    "asset_query",
    "dep_matrix",
    "prereq_discovered",
    "cost_trap",
    "perf_limit",
    "soft_limit",
    "nonfunctional_floor",
    "no_convergence",
    "root_cause_mapped",
    "counterintuitive",
    "protocol_edge",
    "offload_matrix",
    "state_machine_stuck",
    "diagnostic_hazard",
    "silent_failure_suspected",
})
LONG_TERM_CORRECTION_RE = re.compile(
    r"以后(?:都|一律|默认|请)|今后|从现在起|长期(?:规则|约束|偏好)|始终|记住|"
    r"每次(?:都|请)|所有任务.{0,12}(?:都|必须)|always|from now on|going forward|by default|remember",
    re.I,
)


def classify_reuse(args_text: str, paths: list[str]) -> str:
    """判定本轮写出的是可复用资产还是一次性产出。

    一次性产出：落在临时目录，或把现场 IP、资源 ID、密钥写死在赋值里，只解本次问题。
    可复用产出：带参数或环境变量入口，或结构化配置文件，能换环境重复调用而不用改源码。
    判据优先级：参数化 > 诊断配方 > 配置模板 > 顶层硬编码值。
    """
    if REUSE_PARAM_RE.search(args_text):
        return "reusable"
    # 诊断命令成组出现时，现场值是参数不是硬编码：命令形状本身可复用。
    if is_diagnostic_recipe(args_text):
        return "reusable"
    if any(CONFIG_SUFFIX_RE.search(path) for path in paths):
        return "reusable"
    if TEMP_PATH_RE.search(" ".join(paths)):
        return "one_off"
    if ONE_OFF_ASSIGN_RE.search(args_text) and MODULE_LEVEL_RE.search(args_text):
        return "one_off"
    return "reusable"


def classify_turn_asset(turn: dict) -> str:
    """给候选标注本轮资产的可复用性：reusable / one_off / none。"""
    for tool in turn.get("tools") or []:
        if str(tool.get("name") or "").lower() not in WRITE_TOOLS:
            continue
        args_text = _args_text(tool.get("args", ""))
        paths = _arg_paths(tool.get("args", ""))
        paths = [path for path in paths if not lib.is_git_tracked(path)]
        if not any(SCRIPT_SUFFIX_RE.search(path) for path in paths):
            continue
        return classify_reuse(args_text, paths)
    return "none"


def reusable_asset_paths(turn: dict) -> list[str]:
    """抠出本轮产出的可复用资产路径，供资产登记表建档。

    只回可复用的：一次性产出即使落了候选也不进登记表，避免登记表变成临时文件坟场。
    """
    picked: list[str] = []
    for tool in turn.get("tools") or []:
        if str(tool.get("name") or "").lower() not in WRITE_TOOLS:
            continue
        args_text = _args_text(tool.get("args", ""))
        paths = [path for path in _arg_paths(tool.get("args", "")) if SCRIPT_SUFFIX_RE.search(path)]
        if not paths or classify_reuse(args_text, paths) != "reusable":
            continue
        for path in paths:
            if lib.is_git_tracked(path):
                continue
            if path not in picked:
                picked.append(path)
    return picked


def extract_skill_names(turn: dict) -> set[str]:
    """从工具参数里认出被真正调用的 skill 名称。

    只认 `skills/<名>/scripts/` 下的资产入口：读 `SKILL.md` 是查文档，不是编排。
    """
    names: set[str] = set()
    for tool in turn.get("tools") or []:
        for match in SKILL_ASSET_RE.finditer(_text_of(tool.get("args", ""))):
            name = match.group(1)
            if name not in SELF_SKILL_NAMES:
                names.add(name)
    return names


# 项目载体：结论写进这些文件就等于进了权威载体，经验库再记一条只是复述。
# 判定看**参数**：读载体、搜载体都不算。委托类工具一并纳入——写盘发生在子会话里，
# 父轮只留一条 `Agent`/`Task` 调用，参数点名载体路径时结论同样已进载体。
CARRIER_WRITE_TOOLS = (*WRITE_TOOLS, "file_change", *DELEGATE_TOOLS)
# 强载体：工作区与项目里的权威文档及其语义目录，写到这些位置结论就已落地。
# `.harness/**` 覆盖规则、知识库、调研、评测、模板、迭代与本地工作区文档；
# `.ai/**` 覆盖子仓入口、规则与参考资料。
CARRIER_MARKERS = (
    re.compile(r"[\w./\\-]*AGENTS\.md", re.I),
    re.compile(r"[\w./\\-]*SKILL\.md", re.I),
    re.compile(r"[\w./\\-]*(?:CLAUDE|README|CONTRIBUTING)\.md", re.I),
    re.compile(r"[\w./\\-]*\.harness[/\\][\w./\\-]+", re.I),
    re.compile(r"[\w./\\-]*\.ai[/\\][\w./\\-]+", re.I),
    re.compile(r"[\w./\\-]*\.ai-knowledge[/\\][\w./\\-]+", re.I),
    re.compile(r"[\w./\\-]*openspec[/\\][\w./\\-]+", re.I),
)
# 文档目录载体：仓库里的 docs/、doc/、kb/、research/、evals/、templates/、iterations/。
# 这些目录名太泛，只对真写盘工具生效：委托类工具的 prompt 里点到某个 docs 路径很常见，
# 那是「让对方去读」，不是本轮把结论写进了载体。
DOC_CARRIER_MARKERS = (
    re.compile(
        r"(?:^|[\s\"'(\[=:/\\])(?:docs?|kb|knowledge|research|evals?|templates?|iterations?)[/\\][\w./\\-]+",
        re.I,
    ),
)
STRONG_CARRIER_TOOLS = (*WRITE_TOOLS, "file_change")


def detect_carrier_write(turn: dict) -> str:
    """本轮把结论写进了哪个项目载体；没写载体则返回空串。

    只看命令行上的落点：重定向目标（`cat > openspec/…/x.md`）在命令行里，
    写在 here-doc 正文里的路径是脚本内容，不是这一轮真正写到哪里。
    泛化的文档目录只在真写盘工具上认，委托类工具只认强载体。
    """
    for tool in turn.get("tools") or []:
        name = str(tool.get("name") or "").lower()
        if name not in CARRIER_WRITE_TOOLS:
            continue
        args_text = _command_text(tool)
        markers = (*CARRIER_MARKERS, *DOC_CARRIER_MARKERS) if name in STRONG_CARRIER_TOOLS else CARRIER_MARKERS
        for marker in markers:
            match = marker.search(args_text)
            if match:
                return match.group(0).strip("\\/")
    return ""


# 现场触达：外部 CLI 与内核/日志面。命中即说明本轮真的在动环境或读现场数据。
# 只列无歧义的多字母命令名：`ip`、`ss`、`tc`、`az` 这类短词在参数正文里遍地都是，
# 误命中会把讨论轮判成执行轮——判定方向是「宁可判成执行轮」，绝不反向放过。
EXTERNAL_CLI_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:aliyun|aws|gcloud|hcloud|kubectl|helm|etcdctl|consul|systemctl|docker|podman|"
    r"iptables|nft|ovs-vsctl|gwsvc-ctl|netstat|conntrack|ethtool|sysctl|modprobe|tcpdump|tshark|pdump|"
    r"strace|perf|bpftrace|ftrace|curl|wget|ssh|scp|ping|mtr|traceroute|dig|nslookup|redis-cli|mysql|"
    r"psql|ansible|terraform|cmake|make|pytest|unittest|dmesg|journalctl|systeminfo)(?![A-Za-z0-9])",
    re.I,
)
# 现场数据面：路径落在内核、日志、抓包文件上时，读它读的是现场，不是源码。
FIELD_PATH_RE = re.compile(r"/proc/|/sys/|/var/log|\.log\b|\.pcap\b|\.dmp\b|\.etl\b|/dev/", re.I)
# 源码与文档：读这些文件属于「看代码查文档」，不是动现场。
SOURCE_SUFFIX_RE = re.compile(
    r"\.(?:c|h|cc|cpp|cxx|hpp|py|go|rs|java|kt|scala|ts|tsx|js|mjs|cjs|sh|bash|zsh|ps1|bat|cmd|sql|"
    r"md|markdown|txt|rst|json|ya?ml|toml|ini|conf|cfg|tf|j2|tmpl|html|css|scss|less|xml|proto|gradle)\b",
    re.I,
)


def _is_source_reader(tool: dict) -> bool:
    """这个调用是否只是在读源码或文档。"""
    name = str(tool.get("name") or "").strip().lower()
    if name in (*WRITE_TOOLS, "file_change"):
        return False
    if is_narrative_tool(tool):
        return True
    args_text = _args_text(tool.get("args", ""))
    if not args_text:
        return False
    if EXTERNAL_CLI_RE.search(args_text) or FIELD_PATH_RE.search(args_text):
        return False
    if AGENT_TOOLING_RE.search(args_text):
        return True
    if _is_readonly_command(args_text):
        return True
    return _reads_source_file(tool)


def is_discussion_only(turn: dict) -> bool:
    """本轮是否只是「读代码、读文档 + 讨论」：全程没有触达现场。

    这类轮次产出的方案取舍、评审意见与设计问答由 OpenSpec 等项目载体保存，
    没有实测数值、没有错误原文、没有状态异常，落候选只是把设计过程抄进经验库。
    没有工具调用的纯聊天轮不算讨论轮：纠正与长期约束仍要能落候选。
    """
    tools = turn.get("tools") or []
    if not tools:
        return False
    return all(_is_source_reader(tool) for tool in tools)


def candidate_skip_reason(turn: dict, signals: list[str]) -> str | None:
    """Gate candidates on new reusable evidence, not ordinary task activity."""
    signal_set = set(signals)
    if detect_carrier_write(turn) and "asset_script" not in signal_set:
        return "carrier_written"

    correction = detect_correction(turn)
    if correction and LONG_TERM_CORRECTION_RE.search(str(turn.get("user") or "")):
        return None
    # 读代码/读文档 + 讨论的轮次：方案取舍、评审意见与设计问答由 OpenSpec 等项目载体
    # 保存，落候选只是把同一份结论再抄一遍。证据型信号在这类轮次上没有现场可依。
    if is_discussion_only(turn):
        return "discussion_only"
    if signal_set & REUSABLE_EVIDENCE_SIGNALS:
        return None
    return "no_reusable_evidence"


def _recovery_statuses(tools: list[dict]) -> list[tuple[str, str]]:
    """按调用顺序给出「命令族 + 状态」，并把「不值得当恢复策略」的失败降级。

    CLI 用法错误与测试红绿降级：它们由工具自身与开发循环产生，重跑一次就能复现，
    落到「恢复策略」里只会把长会话的常态记成经验。
    顺序必须保持原样：「连续失败」指的是同一命令族中间没有成功调用，重新分组会把它算成连续。
    """
    statuses: list[tuple[str, str]] = []
    for tool in tools:
        status = str(tool.get("status") or "")
        if status == "error":
            error_text = f"{tool.get('error') or ''} {tool.get('error_text_hint') or ''}"
            if USAGE_ERROR_RE.search(error_text) or TEST_RESULT_RE.search(error_text):
                status = "not_recovery"
        statuses.append((_family_key(tool), status))
    return statuses


def detect_signals(turn: dict) -> list[str]:
    """返回过程与证据型信号；候选准入由 candidate_skip_reason 决定。"""
    signals: list[str] = []
    tools = turn.get("tools") or []
    user_text = str(turn.get("user") or "")
    outcome = str(turn.get("outcome") or "success")

    # 同一命令族内出错之后成功，说明存在可复用的恢复策略。
    # 键必须带命令族：codex 全轮只有一个 exec，按工具名配对等于「本轮出过错又成功过」。
    recovered = False
    retry_loop = False
    running_failures: dict[str, int] = {}
    failed_families: set[str] = set()
    for key, status in _recovery_statuses(tools):
        if status == "error":
            running_failures[key] = running_failures.get(key, 0) + 1
            failed_families.add(key)
            if running_failures[key] >= RETRY_THRESHOLD:
                retry_loop = True
            continue
        running_failures[key] = 0
        if status == "ok" and key in failed_families:
            recovered = True
            failed_families.discard(key)
    if recovered:
        signals.append("error_recovered")

    # 同工具同参数重复调用，且至少一次没打通：两次都成功只是重复执行（重复跑测试、重复读文件）。
    counts: dict[str, int] = {}
    ok_only: dict[str, bool] = {}
    for tool in tools:
        signature = _tool_signature(tool)
        counts[signature] = counts.get(signature, 0) + 1
        ok_only[signature] = ok_only.get(signature, True) and _reached_ok(tool)
    if any(count >= RETRY_THRESHOLD and not ok_only[signature] for signature, count in counts.items()):
        signals.append("retry")

    # 同一命令族连续失败：原样重试之外还有参数摸索，这条路只有失败次数看得见。
    if retry_loop:
        signals.append("retry_loop")

    correction = detect_correction(turn)
    if correction:
        signals.append(correction["signal"])

    # 跨两个及以上 skill 编排（调用别的 skill 的资产），或者工具种类繁多。
    if len(extract_skill_names(turn)) >= MULTI_SOURCE_THRESHOLD:
        signals.append("multi_source")
    elif len({tool.get("name") for tool in tools}) >= 4:
        signals.append("multi_source")

    # 时长不再是信号：它与轮次价值无因果，只作为候选 cost.duration_ms 保留。
    # 环境名词同样不再落 env_hazard——见 HAZARD_ACTION_KEYWORDS 上的说明。

    if outcome in ("aborted", "incomplete"):
        signals.append("failure")

    for tool in tools:
        name = str(tool.get("name") or "")
        lowered = name.lower()
        args_text = _args_text(tool.get("args", ""))
        # 判据取词面：叙述承载类工具（Agent/SendMessage/AskUserQuestion…）的参数是正文，
        # 只有命令与写盘类工具的参数算现场。
        judge_text = _judgement_args(tool)
        reached_ok = _reached_ok(tool)

        # 依赖与环境安装成功，说明环境前置条件已经摸清。
        if reached_ok and (INSTALL_COMMAND_RE.search(judge_text) or INSTALL_TOOL_RE.search(lowered)):
            signals.append("dep_fix")

        # 本轮写出可复用资产：脚本、配置模板或图示。
        # 存在性不是判据，可复用性才是：只解本次问题的一次性产出不落候选。
        if lowered in WRITE_TOOLS:
            paths = _arg_paths(tool.get("args", ""))
            paths = [path for path in paths if not lib.is_git_tracked(path)]
            if any(SCRIPT_SUFFIX_RE.search(path) for path in paths) and (
                classify_reuse(args_text, paths) == "reusable"
            ):
                signals.append("asset_script")
            if _flags(args_text, DIAGRAM_MARKERS):
                signals.append("asset_diagram")

        # 环境复现或服务启动成功：下次不必重摸一遍。
        if reached_ok and (SETUP_COMMAND_RE.search(judge_text) or SETUP_TOOL_RE.search(lowered)):
            signals.append("env_recovery")
        elif SETUP_HINT_RE.search(user_text) and SETUP_TOOL_RE.search(lowered):
            signals.append("env_recovery")

        # 变更前先兜底：快照、备份、灰度、限速、dry-run。
        if _flags(judge_text, BLAST_GUARD_KEYWORDS):
            signals.append("blast_radius_guard")

        # 动变更前留现场：抓包、导出日志、配置快照。
        if EVIDENCE_CAPTURE_RE.search(judge_text):
            signals.append("evidence_capture")

        # 不可逆或高危动作：只认工具参数里的动作本身，
        # 不看用户消息里的「线上」「回滚」——那是背景叙述，不是本轮动的动作。
        if _flags(judge_text, HAZARD_ACTION_KEYWORDS):
            signals.append("env_hazard")

    signals.extend(_detect_reasoning(turn, tools, user_text))
    signals.extend(_detect_recipe(turn, tools))
    signals.extend(_detect_prereq(turn, tools))
    signals.extend(_detect_nonfunctional(turn, tools))
    signals.extend(_detect_ops(turn, tools))
    signals.extend(_detect_dep_matrix(tools))
    signals.extend(_detect_silent_failure(turn, tools, user_text))
    signals.extend(_detect_deep_layer(turn, tools, user_text))
    signals.extend(_grounded_verdict(turn))

    return _dedupe(signals)


def _detect_deep_layer(turn: dict, tools: list[dict], user_text: str) -> list[str]:
    """深水区四类：卸载矩阵、观察者效应、协议边缘、控制面状态机。"""
    found: list[str] = []
    assistant = str(turn.get("assistant") or "")
    narrative = f"{user_text} {assistant}"
    haystack = narrative + " " + " ".join(_tool_evidence(tool) for tool in tools)

    # 卸载特性矩阵：同一特性在不同底座上表现不同，只有出现矩阵语境才算经验。
    if OFFLOAD_FEATURE_RE.search(haystack) and (
        OFFLOAD_CMD_RE.search(haystack)
        or OFFLOAD_ANOMALY_RE.search(haystack)
        or PLATFORM_CONTEXT_RE.search(haystack)
    ):
        found.append("offload_matrix")

    # 观察者效应：高强度观测 + 高负载语境 + 退化症状，三者缺一不落。
    if HEAVY_OBSERVER_RE.search(haystack) and HIGH_LOAD_RE.search(haystack) and DEGRADATION_RE.search(haystack):
        found.append("diagnostic_hazard")
    else:
        for tool in tools:
            command = _args_text(tool.get("args", ""))
            if not HEAVY_OBSERVER_RE.search(command):
                continue
            capture = CAPTURE_CMD_RE.search(command)
            # 抓到单条命令：命令本身没有包数或时长上限，就是高风险动作。
            if capture and not UNBOUNDED_CAPTURE_RE.search(capture.group(0)):
                found.append("diagnostic_hazard")
                break

    # 协议边缘：现象加闭环动作，缺动作说明只是抱怨，不是经验。
    # 现象词取现场证据（错误原文、非文档结果）；闭环词只认变更类调用的参数——
    # 检索模式里出现 `mtu`（rg -n "mtu" ...）不是「落到调整」。
    if PROTOCOL_EDGE_RE.search(_evidence_text(tools)) and PROTOCOL_FIX_RE.search(_change_args(tools)):
        found.append("protocol_edge")

    # 控制面状态机：轮询不动 或 状态冲突，且最终改顺序或加等待才走通。
    # 解决动作常写在结论段或工具返回里（「先解绑再绑定」「等待 5 秒后重试」），三处一起看。
    found.extend(_detect_state_machine(tools, f"{narrative} {haystack}"))
    return found


def _detect_state_machine(tools: list[dict], narrative: str) -> list[str]:
    """资源级联与状态机卡死：轮询同一目标状态不变，或状态冲突后改顺序成功。"""
    polls: dict[str, int] = {}
    conflict = False
    for tool in tools:
        args_text = _command_text(tool)
        result = _judgement_result(tool)
        if STATE_CONFLICT_RE.search(f"{result} {tool.get('error') or ''}"):
            conflict = True
        # 轮询必须落在控制面 CLI 的状态读取上：读文件、跑脚本带 status 字样的参数不算轮询。
        if not (STATE_POLL_CMD_RE.search(args_text) and STATE_POLL_CLI_RE.search(args_text)):
            continue
        target = ",".join(sorted(resource_ids(args_text))) or _tool_signature(tool)
        polls[target] = polls.get(target, 0) + 1
    stuck = any(count >= STATE_POLL_THRESHOLD for count in polls.values())
    if (stuck or conflict) and STATE_RESOLUTION_RE.search(narrative):
        return ["state_machine_stuck"]
    return []


def _detect_silent_failure(turn: dict, tools: list[dict], user_text: str) -> list[str]:
    """工具返回成功但结果不符预期：网络排障里这类经验比显式报错更值钱。

    两条路并集，任一成立即命中：
    - 动作反转：成功之后又给同一工具加诊断开关，或走「变更→清理→再变更」的回头路
    - 状态断言：成功之后紧接着读状态，且人给出疑问或否定结论
    """
    # 动作反转：成功之后加诊断开关，说明第一次的成功没让人放心。
    # 必须落在同一命令族的后续调用上：不同命令里的 --force 与本轮结论无关。
    for index, tool in enumerate(tools):
        if not _reached_ok(tool):
            continue
        key = _family_key(tool)
        for later in tools[index + 1 :]:
            if _family_key(later) != key:
                continue
            if INTENSIFIER_RE.search(_command_text(later)):
                return ["silent_failure_suspected"]
        for later in tools[index + 1 :]:
            if not _reached_ok(later):
                continue
            if is_diagnostic_recipe(_command_text(later)):
                if NEGATIVE_CONCLUSION_RE.search(f"{turn.get('assistant') or ''} {user_text}"):
                    return ["silent_failure_suspected"]
    return []


def _detect_dep_matrix(tools: list[dict]) -> list[str]:
    """同一依赖换过版本才装成：版本矩阵是排出来的，不是查出来的。

    只记录失败过的版本不算矩阵，必须有一条装成功，否则留下的是「装不上」而不是「怎么装上」。
    """
    versions: dict[str, set[str]] = {}
    succeeded = False
    for tool in tools:
        args_text = _args_text(tool.get("args", ""))
        if not INSTALL_COMMAND_RE.search(args_text):
            continue
        if _reached_ok(tool):
            succeeded = True
        for name, version in SPEC_PIN_RE.findall(args_text):
            versions.setdefault(name.lower(), set()).add(version)
    if succeeded and any(len(found) >= 2 for found in versions.values()):
        return ["dep_matrix"]
    return []


def _detect_reasoning(turn: dict, tools: list[dict], user_text: str) -> list[str]:
    """族 B 的跨轮推理信号，以及优先级最高的两条判据型信号。"""
    found: list[str] = []
    assistant = str(turn.get("assistant") or "")
    prev_user = str(turn.get("prev_user") or "")

    # 改换工具链后成功：上一轮失败、这一轮换法走通，只有当事轮看得见。
    previous_failed = str(turn.get("prev_outcome") or "") in ("aborted", "incomplete")
    errored_families = set()
    for tool in tools:
        if str(tool.get("status") or "") != "error":
            continue
        match = DIAGNOSTIC_FAMILY_RE.search(_args_text(tool.get("args", "")))
        errored_families.add(match.group(0).lower() if match else str(tool.get("name") or "").lower())
    if (errored_families or previous_failed) and any(_reached_ok(tool) for tool in tools):
        switched = any(
            _reached_ok(tool)
            and (match := DIAGNOSTIC_FAMILY_RE.search(_args_text(tool.get("args", ""))))
            and match.group(0).lower() not in errored_families
            for tool in tools
        )
        if switched and errored_families:
            found.append("fallback_switch")

    # 后一轮推翻先前结论：必须是明确翻案措辞，日常的「之前」「改为」不算。
    if _flags(f"{assistant} {user_text}", REVERSAL_MARKERS):
        found.append("decision_reversed")

    # 评审被打回，附带理由。
    rejected = ("打回", "不通过", "驳回", "评审意见", "需要修改", "拒绝合并", "changes requested")
    if _flags(assistant, rejected) or _flags(user_text, rejected):
        found.append("review_rejected")

    # 现象与根因的映射：错误原文与结论句式必须同时出现，配不上不落。
    error_texts = " ".join(
        str(tool.get("error") or "") + " " + str(tool.get("error_text_hint") or "") for tool in tools
    )
    has_error = bool(error_texts.strip()) or previous_failed
    if has_error and _flags(f"{assistant} {prev_user}", CONCLUSION_MARKERS):
        found.append("root_cause_mapped")
    if _flags(f"{assistant} {prev_user}", COUNTERINTUITIVE_MARKERS):
        found.append("counterintuitive")
    return found


def is_query_statement(args_text: str) -> bool:
    """日志或指标查询语句，且带具体字段口径。

    源码检索里的 `|stats` 不算：`Select-String -Pattern 'dests|stats|refcnt'`、
    `rg "^(...|stats)\\.h"` 是看代码，不是查流量日志，判成查询语句会把读源码的轮次
    误认成资产产出。
    """
    if _is_readonly_command(args_text):
        return False
    return bool(QUERY_STATEMENT_RE.search(args_text))


def _detect_recipe(turn: dict, tools: list[dict]) -> list[str]:
    """族 C 的命令配方、查询语句与探测纪律。"""
    found: list[str] = []
    families: set[str] = set()
    probes = 0
    query_hit = False
    for tool in tools:
        if is_narrative_tool(tool):
            continue
        args_text = _command_text(tool)
        for match in DIAGNOSTIC_FAMILY_RE.finditer(args_text):
            families.add(match.group(0).lower())
        probes += len(PROBE_FAMILY_RE.findall(args_text))
        if is_query_statement(args_text):
            query_hit = True
    if len(families) >= DIAGNOSTIC_THRESHOLD:
        found.append("asset_recipe")
    if query_hit:
        found.append("asset_query")
    if probes >= PROBE_THRESHOLD:
        found.append("probe_discipline")
    return found


def is_diagnostic_recipe(args_text: str) -> bool:
    """命令成组出现即为诊断配方。

    抓包、看路由、查会话表这类命令天生带现场值（`tcpdump -i eth0 host 10.0.0.1`），
    不能按「写死 IP」判成一次性：命令的形状可复用，变的只是参数。
    出现两个诊断族，或一次探测族加一个诊断族，即算成组。
    """
    families = {match.group(0).lower() for match in DIAGNOSTIC_FAMILY_RE.finditer(args_text)}
    probes = PROBE_FAMILY_RE.findall(args_text)
    return len(families) >= 2 or (len(families) >= 1 and len(probes) >= 1)


def is_volatile_field(name: str) -> bool:
    """字段名是否每次调用都变：动态字段不能进幂等对比。

    `ts` 这类两字母字段必须整名匹配：它是 `status` 的子串，按包含判断会把真字段剔掉。
    """
    lowered = name.replace("_", "").replace("-", "").lower()
    if lowered in ("ts", "id", "no", "seq"):
        return True
    return any(marker in lowered for marker in VOLATILE_FIELD_MARKERS)


def normalized_result(tool: dict) -> str:
    """把工具返回值规范化后再比对：剔掉每次都变的字段。

    返回体里的 timestamp、req_id、trace_id、耗时天生不同，直接比字符串必然命中幂等不一致。
    优先只比 `字段=值` 这类稳定字段；抽不出字段时，只有短小标量结果可比——构建日志、测试日志
    这类长输出天生每次不同，拿它们比等于把「每次都有输出」记成「幂等被破坏」。
    """
    raw = str(tool.get("result") or "").strip()
    if not raw:
        return ""
    pairs = [
        f"{name.lower()}={value}"
        for name, value in FIELD_ASSIGN_RE.findall(raw)
        if not is_volatile_field(name)
    ]
    if pairs:
        return "|".join(sorted(pairs))
    if any(is_volatile_field(name) for name, _ in FIELD_ASSIGN_RE.findall(raw)):
        return ""
    if len(raw) <= SCALAR_RESULT_CHARS and raw.count("\n") < SCALAR_RESULT_LINES:
        return raw
    return ""


def _detect_prereq(turn: dict, tools: list[dict]) -> list[str]:
    """族 D 的前置条件信号：先把坑踩明白，再谈复用。"""
    found: list[str] = []
    prereq_failed = False
    for tool in tools:
        error = f"{tool.get('error') or ''} {tool.get('error_text_hint') or ''}"
        if PREREQ_ERROR_RE.search(error):
            prereq_failed = True
    # 前置条件坑只在「踩过并走通」时才算经验：光报错是故障，不是经验。
    if prereq_failed and any(_reached_ok(tool) for tool in tools):
        found.append("prereq_discovered")

    # 变更后读状态仍是旧值：数据面没跟上控制面。两边都要有，缺一边都可能误判。
    mutated = any(
        mutation_action(_command_text(tool))
        and _reached_ok(tool)
        for tool in tools
    )
    # 旧值只从现场输出里读：读源码、读文档、检索仓库拿回来的正文里遍地是 `unchanged`、
    # `pending` 这类词（源码注释里的状态枚举），拿它们判「状态没跟上」是把转述当实测。
    stale_read = any(
        STATUS_CALL_RE.search(_command_text(tool))
        and OLD_STATE_RE.search(_judgement_result(tool))
        for tool in tools
    )
    if mutated and stale_read:
        found.append("no_convergence")

    # 同一操作重复执行结果不一致：幂等被破坏，这类坑必须留。
    for index, tool in enumerate(tools):
        for later in tools[index + 1 :]:
            if later.get("name") != tool.get("name") or _tool_signature(later) != _tool_signature(tool):
                continue
            first, second = normalized_result(tool), normalized_result(later)
            if first and second and first != second:
                found.append("idempotency_mismatch")
    # 同一目标连续多次尝试才收敛。
    attempts: dict[str, int] = {}
    for tool in tools:
        for identifier in resource_ids(_args_text(tool.get("args", ""))):
            attempts[identifier] = attempts.get(identifier, 0) + 1
    if any(count >= CONVERGENCE_ATTEMPT_THRESHOLD for count in attempts.values()):
        found.append("convergence_delay")
    # 用户就同一问题反复追问。
    user_text = str(turn.get("user") or "")
    history = [str(item) for item in (turn.get("prev_users") or []) if str(item).strip()]
    if user_text and history:
        similar = sum(1 for item in history if _similarity(item, user_text) >= QA_SIMILARITY)
        if similar + 1 >= QA_THRESHOLD:
            found.append("qa_reflex")
    return found


def _is_measure_value(text: str) -> bool:
    """量词候选是否真的是量词：年份与长数字（哈希、ID）不算。"""
    value = text.strip()
    if MEASURE_YEAR_RE.match(value):
        return False
    return not MEASURE_LONG_ID_RE.search(value)


def _has_word_marker(text: str, markers) -> bool:
    """语境词是否真的作为词出现。

    源码标识符里遍地是 `MAX_DESTS`、`num_dests_limit`、`tcp_rmem_max`：把它们当成
    「上限语境」，读代码的轮次就会配上附近的数字落 perf_limit。ASCII 词要求前后不是
    字母数字或下划线，中文词仍按子串匹配。
    """
    for marker in markers:
        if not (marker.isascii() and marker.isalpha()):
            if marker in text:
                return True
            continue
        for match in re.finditer(marker, text, re.I):
            before = text[match.start() - 1] if match.start() else ""
            after = text[match.end()] if match.end() < len(text) else ""
            if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
                continue
            return True
    return False


def _detect_nonfunctional(turn: dict, tools: list[dict]) -> list[str]:
    """族 F 的性能、成本与内核下限：文档常缺的实测数值。

    取词面是现场证据（工具参数、错误原文、非文档结果）：总结文档里的「性能提升 50%」
    是转述，把它当硬限会产出「文档里提到的数字」这种候选。
    """
    found: list[str] = []
    haystack = _evidence_text(tools)
    # 只认「量词（条/个/次）+ 实测口径词」的组合：单独一个「次」出现在闲聊里不算数值。
    measures = [match.group(0) for match in ENV_MEASURE_RE.finditer(haystack)]
    if any(_is_measure_value(value) for value in measures) and _flags(haystack, ENV_MEASURE_HINTS):
        found.append("cost_trap")
    # 数值必须带单位，裸数字不算：端口、ID、序号在云网络里满地都是。
    # 硬限制（平台天花板、协议上限）与软限制（本环境经验值）分开：
    # 前者转监控告警规则，后者转开发前置约束，混在一起复盘时无法分流。
    # 中文量词分支（三条、25 个）是计数不是容量，不能当硬限的数值来源。
    values = [match for match in VALUE_UNIT_RE.finditer(haystack) if _is_measure_value(match.group(0))]
    if _has_word_marker(haystack, HARD_LIMIT_MARKERS):
        # 硬限语境下裸数字也算（`65535 上限`、`端口耗尽 65535`），但必须同窗口：
        # 代码与命令里满是 `MAX`、`limit` 和 ID/时间戳，隔开几十个字符配成一对就是误报。
        for match in BARE_LARGE_NUMBER_RE.finditer(haystack):
            if MEASURE_LONG_ID_RE.search(match.group(0)):
                continue
            window = haystack[max(0, match.start() - 40) : match.end() + 10]
            if _has_word_marker(window, HARD_LIMIT_MARKERS):
                found.append("perf_limit")
                break
    if PERFORMANCE_UNIT_RE.search(haystack) and _has_word_marker(haystack, PERFORMANCE_MARKERS):
        # 性能量纲的数值不要求语境在数值附近：`压测到 5000 pps 时开始丢包` 语境在后半句。
        found.append("perf_limit")
    for match in values:
        window = haystack[max(0, match.start() - 40) : match.end() + 10]
        if _has_word_marker(window, HARD_LIMIT_MARKERS):
            found.append("perf_limit")
            break
        if _has_word_marker(window, SOFT_LIMIT_MARKERS):
            found.append("soft_limit")
            break
        if CONNECTION_CAPACITY_RE.search(window) and _flags(window, ("满", "耗尽", "上限", "超")):
            found.append("perf_limit")
            break
    # 决策取舍的现场是人的决策语句：判据词必须与「二选一 + 决定」同句，
    # 只提到「成本/配额/SLA」不算取舍。
    if _cost_decision_made(f"{str(turn.get('user') or '')} {str(turn.get('assistant') or '')}"):
        found.append("cost_decision")
    if NONFUNCTIONAL_FLOOR_RE.search(haystack):
        found.append("nonfunctional_floor")
    return found


def _cost_decision_made(text: str) -> bool:
    """判据词 + 同一窗口里的取舍结构与决定判词。"""
    for keyword in COST_DECISION_MARKERS:
        for match in re.finditer(re.escape(keyword), text, re.I):
            window = text[max(0, match.start() - 80) : match.end() + 80]
            if TAKEOFF_STRUCTURE_RE.search(window) and DECISION_VERB_RE.search(window):
                return True
    return False


def _grounded_verdict(turn: dict) -> list[str]:
    """结论锚定源码行号：文档与源码都查不到的对照结论。

    本轮结论出现 ≥3 处 `文件:行号` 引用，说明做过跨源码与历史的实读比对，
    产出的判据无法从文档或 `--help` 恢复——正是判定准则里的「现象与原因的映射」。
    """
    assistant = str(turn.get("assistant") or "")
    if len(FILE_REF_RE.findall(assistant)) >= FILE_REF_THRESHOLD:
        return ["grounded_verdict"]
    return []


def _detect_ops(turn: dict, tools: list[dict]) -> list[str]:
    """族 G 的批量变更：同一工具对不同资源连续动刀，回滚面与残留值得记。"""
    found: list[str] = []
    per_tool: dict[str, set[str]] = {}
    for tool in tools:
        args_text = _command_text(tool)
        if not mutation_action(args_text):
            continue
        identifiers = resource_ids(args_text)
        if not identifiers:
            continue
        per_tool.setdefault(str(tool.get("name") or ""), set()).update(identifiers)
    if any(len(ids) >= BATCH_MUTATION_THRESHOLD for ids in per_tool.values()):
        found.append("batch_mutation")
    return found

def _dedupe(signals: list[str]) -> list[str]:
    """同轮同一信号只留一条：候选上的信号标签是给人看的，重复没有信息量。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for signal in signals:
        if signal not in seen:
            seen.add(signal)
            ordered.append(signal)
    return ordered


def detect_correction(turn: dict) -> dict | None:
    """判定人类纠正：返回 {signal, kind, user_said, agent_did}，未命中返回 None。

    文本判定只对短消息做（≤ `CORRECTION_MAX_CHARS`）：纠正是对 agent 上一轮做法的反应，
    而贴进来的周报、日志、需求模板里到处都是「不要」「改成」。纯续跑口令（继续、接着来）
    同样不算纠正——用户只是让 agent 接着干，没有给出新版本。
    """
    user_text = str(turn.get("user") or "")
    if not user_text:
        return None
    tools = turn.get("tools") or []
    agent_did = _describe_agent_action(turn)

    # 权限拒绝是结构化证据，不受文本长度限制：用户只回「继续」也算。
    if any(tool.get("status") == "denied" for tool in tools):
        return _correction("user_correction", "permission_denied", user_text, agent_did)
    if _is_continuation(user_text) or len(user_text) > CORRECTION_MAX_CHARS:
        return None

    previous = str(turn.get("prev_user") or "")
    if any(marker in user_text for marker in DENIAL_MARKERS):
        return _correction("user_correction", "explicit_denial", user_text, agent_did)
    if any(marker in user_text for marker in RESTATE_MARKERS) or (
        previous
        and _similar_lengths(previous, user_text)
        and _similarity(previous, user_text) >= 0.6
        and previous.strip() != user_text.strip()
    ):
        return _correction("user_correction", "restated_request", user_text, agent_did)
    if any(marker in user_text for marker in REJECTION_MARKERS):
        return _correction("user_correction", "plan_rejected", user_text, agent_did)
    if TAKEOVER_RE.search(user_text):
        return _correction("user_override", "manual_takeover", user_text, agent_did)
    if any(marker in user_text for marker in ANSWER_MARKERS) and CONCRETE_VALUE_RE.search(user_text):
        return _correction("user_override", "gave_answer", user_text, agent_did)
    return None


def _is_continuation(text: str) -> bool:
    """整句只是「继续」「接着来」这类口令；带了新诉求就不算。"""
    cleaned = re.sub(r"[\s，。,.!！?？~～]+", "", text or "").lower()
    if not cleaned or len(cleaned) > 6:
        return False
    return any(marker in cleaned for marker in CONTINUATION_MARKERS)


def _similar_lengths(left: str, right: str) -> bool:
    """两条消息长度可比：7KB 周报与 4 字回复不构成「同一诉求的重述」。"""
    longer = max(len(left.strip()), len(right.strip()))
    if not longer:
        return False
    return min(len(left.strip()), len(right.strip())) / longer >= CORRECTION_MIN_LENGTH_RATIO


def _correction(signal: str, kind: str, user_said: str, agent_did: str) -> dict:
    return {"signal": signal, "kind": kind, "user_said": user_said, "agent_did": agent_did}


def _describe_agent_action(turn: dict) -> str:
    """用一句话描述 agent 本轮做了什么，供复盘时与被纠正的原做法对照。"""
    tools = turn.get("tools") or []
    if not tools:
        return str(turn.get("assistant") or "")[:200]
    names = [str(tool.get("name") or "unknown") for tool in tools]
    summary = "、".join(names[:5])
    if len(names) > 5:
        summary += f" 等 {len(names)} 次调用"
    return summary
