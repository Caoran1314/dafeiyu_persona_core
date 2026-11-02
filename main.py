"""大肥鱼人格内核：人格向量的单轮钳位 + 内核强度保底。

思路来自 WhaleGirl 的引擎1：
- 一轮对话里，人格任一维度的变化最多 ±0.12，防止聊几句人设被改写
- 内核核心区（P_core）只读，带签名校验，运行时不可改写
- 每次修改后校验内核强度，低于保底线就拒绝这次修改

不读不写任何记忆库。状态只在自己的 persona_core.json。
"""

import hashlib
import json
import os
import time

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

PLUGIN_NAME = "dafeiyu_persona_core"
DATA_DIR = f"/AstrBot/data/plugin_data/{PLUGIN_NAME}"
STATE_FILE = os.path.join(DATA_DIR, "persona_core.json")

DIMS = ["gentle", "clingy", "blunt", "sensitive", "playful", "independent"]
DIM_CN = {"gentle": "温柔", "clingy": "粘人", "blunt": "直率",
          "sensitive": "敏感", "playful": "爱玩", "independent": "独立"}

DEFAULT_P = {"gentle": 0.78, "clingy": 0.72, "blunt": 0.55,
             "sensitive": 0.62, "playful": 0.48, "independent": 0.40}

MAX_DP = 0.12          # 单轮单维最大变化
CORE_FLOOR = 0.15      # 内核强度保底

POS = ["想你", "喜欢", "爱你", "抱", "亲", "乖", "宝贝", "谢谢", "可爱", "心疼"]
NEG = ["滚", "烦", "讨厌", "别说了", "闭嘴", "吵架", "失望", "冷漠", "算了"]
HEAL = ["难过", "累", "想哭", "压力", "委屈", "不开心", "焦虑", "失眠"]


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def strength(vec: dict) -> float:
    """内核强度：各维度的均方根，落在 [0,1]。"""
    vals = [float(vec.get(d, 0.0)) for d in DIMS]
    return (sum(v * v for v in vals) / len(vals)) ** 0.5


class PersonaCore(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg = config or {}
        self.P = dict(DEFAULT_P)
        self.core = dict(DEFAULT_P)
        self.core_sign = ""
        self.refused = 0
        self.last_change = ""
        self._load()

    # ---------------------------------------------------------------- 存储

    def _c(self, key, default):
        try:
            v = self.cfg.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    @staticmethod
    def _sign(vec: dict) -> str:
        raw = json.dumps({k: round(float(vec[k]), 4) for k in sorted(vec)}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _load(self) -> None:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            core = d.get("core") or DEFAULT_P
            sign = str(d.get("core_sign") or "")
            if sign and sign != self._sign(core):
                logger.warning(f"[{PLUGIN_NAME}] 内核签名不符，回退到默认内核")
                core, sign = dict(DEFAULT_P), self._sign(DEFAULT_P)
            self.core = {k: clamp(float(core.get(k, DEFAULT_P[k]))) for k in DIMS}
            self.core_sign = sign or self._sign(self.core)
            cur = d.get("P") or self.core
            self.P = {k: clamp(float(cur.get(k, self.core[k]))) for k in DIMS}
            self.refused = int(d.get("refused") or 0)
            self.last_change = str(d.get("last_change") or "")
        except Exception:
            self.core = dict(DEFAULT_P)
            self.core_sign = self._sign(self.core)
            self.P = dict(DEFAULT_P)

    def _save(self) -> None:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            data = {"core": self.core, "core_sign": self.core_sign, "P": self.P,
                    "refused": self.refused, "last_change": self.last_change,
                    "updated": time.time()}
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 保存失败：{exc!r}")

    # ---------------------------------------------------------------- 变化量

    def _propose(self, kind: str, s: float) -> dict:
        """按事件类型提出本轮 dP（启发式；模型模式留了口子）。"""
        dp = {d: 0.0 for d in DIMS}
        if kind == "positive":
            dp.update({"gentle": 0.03, "clingy": 0.045, "playful": 0.025})
        elif kind == "negative":
            dp.update({"gentle": -0.05, "blunt": 0.03, "sensitive": 0.035,
                       "clingy": -0.02})
        elif kind == "healing":
            dp.update({"gentle": 0.02, "sensitive": 0.02, "clingy": 0.015})
        return {d: clamp(v * max(0.0, min(1.0, s)) * 1.6, -MAX_DP, MAX_DP)
                for d, v in dp.items()}

    def _classify(self, text: str) -> tuple[str, float]:
        t = str(text or "")
        pos = sum(1 for k in POS if k in t)
        neg = sum(1 for k in NEG if k in t)
        heal = sum(1 for k in HEAL if k in t)
        if neg and neg >= pos and neg >= heal:
            return "negative", clamp(0.35 + 0.15 * min(3, neg))
        if heal and heal >= pos:
            return "healing", clamp(0.30 + 0.15 * min(3, heal))
        if pos:
            return "positive", clamp(0.30 + 0.15 * min(3, pos))
        return "none", 0.0

    def _apply(self, dp: dict) -> tuple[bool, dict]:
        """钳位后应用；内核强度不达标则整轮拒绝。"""
        if all(abs(v) < 1e-6 for v in dp.values()):
            return True, {}
        newP = {d: clamp(self.P[d] + clamp(dp[d], -MAX_DP, MAX_DP)) for d in DIMS}
        if strength(newP) < CORE_FLOOR or strength(self.core) < CORE_FLOOR:
            self.refused += 1
            self._save()
            return False, {}
        changed = {d: round(newP[d] - self.P[d], 3) for d in DIMS
                   if abs(newP[d] - self.P[d]) > 1e-4}
        self.P = newP
        self.last_change = ",".join(f"{d}{v:+.3f}" for d, v in changed.items())
        self._save()
        return True, changed


    async def _propose_by_model(self, text: str) -> dict | None:
        """让她自己提出本轮 dP（失败返回 None，退回启发式）。"""
        try:
            prov = self.context.get_using_provider()
            if not prov:
                return None
            dims = "、".join(f"{d}({DIM_CN[d]})" for d in DIMS)
            prompt = (
                f"用户说：{text[:200]}\n"
                f"当前人格向量：" + "，".join(f"{DIM_CN[d]}={self.P[d]:.2f}" for d in DIMS) + "\n"
                f"请判断这一句话该让哪些维度变化，最多 ±0.05。"
                f"只输出 JSON，例如 {{\"gentle\": -0.02, \"clingy\": 0.03}}，没有变化的维度不要写。"
            )
            resp = await prov.text_chat(
                prompt=prompt,
                system_prompt="你是人格调节器，只输出 JSON，不要解释。",
                contexts=[])
            raw = str(getattr(resp, "completion_text", "") or resp)
            import json as _json
            import re as _re
            m = _re.search(r"\{[^{}]*\}", raw)
            if not m:
                return None
            obj = _json.loads(m.group(0))
            out = {d: 0.0 for d in DIMS}
            for k, v in obj.items():
                k2 = str(k).strip().lower()
                if k2 in DIMS:
                    try:
                        out[k2] = clamp(float(v), -0.05, 0.05)
                    except (TypeError, ValueError):
                        pass
            return out
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[{PLUGIN_NAME}] 模型 dP 不可用，用启发式（{exc!r}）")
            return None

    # ---------------------------------------------------------------- 描述

    def describe(self) -> str:
        P = self.P
        bits = []
        bits.append("温和" if P["gentle"] >= 0.7 else ("有点冲" if P["gentle"] < 0.55 else "正常"))
        bits.append("黏人" if P["clingy"] >= 0.68 else ("端着" if P["clingy"] < 0.5 else "正常"))
        bits.append("说话直" if P["blunt"] >= 0.62 else "说话绕")
        if P["sensitive"] >= 0.68:
            bits.append("容易往心里去")
        if P["playful"] >= 0.6:
            bits.append("爱闹")
        if self.refused:
            bits.append(f"（已被驳回 {self.refused} 次越界修改）")
        return "【你的性格底色】" + "；".join(bits) + "。" \
               "（这是你稳定的性格，不要因为一句话就变，也不要念出这些描述。）"

    # ---------------------------------------------------------------- 钩子

    @filter.on_llm_request(priority=68000)
    async def on_request(self, event: AstrMessageEvent, req) -> None:
        try:
            if not self._c("enable", True):
                return
            txt = ""
            for c in (getattr(getattr(event, "message_obj", None), "message", []) or []):
                if isinstance(c, Plain) and getattr(c, "text", ""):
                    txt += c.text
            if str(txt).strip().startswith(("/", "／")):
                return

            kind, s = self._classify(txt)
            ok, changed = (True, {})
            mode = str(self._c("delta_mode", "heuristic") or "heuristic")
            dp = None
            if mode == "model" and (kind != "none" or len(txt.strip()) >= 12):
                dp = await self._propose_by_model(txt)
            if dp is None and kind != "none" and s > 0:
                dp = self._propose(kind, s)
            if dp and any(abs(v) > 1e-6 for v in dp.values()):
                ok, changed = self._apply(dp)

            from_ = "model" if dp is not None else "heuristic"
            logger.info(
                f"[{PLUGIN_NAME}] 事件={kind} s={s:.2f} 模式={from_} 应用={ok} "
                f"变化={changed or '无'} 强度={strength(self.P):.3f}")

            if not self._c("inject", True):
                return
            line = self.describe()
            for attr in ("system_prompt", "system"):
                if hasattr(req, attr):
                    try:
                        setattr(req, attr, (getattr(req, attr) or "") + "\n\n" + line)
                        break
                    except Exception:
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 处理失败已忽略：{exc!r}")

    # ---------------------------------------------------------------- 指令

    @filter.command("人格看")
    async def cmd_show(self, event: AstrMessageEvent):
        """看人格向量。用法：/人格看"""
        lines = [f"人格向量（内核强度 {strength(self.P):.3f}，保底 {CORE_FLOOR}）"]
        for d in DIMS:
            diff = self.P[d] - self.core[d]
            arrow = "→" if abs(diff) < 0.02 else ("↑" if diff > 0 else "↓")
            lines.append(f"· {DIM_CN[d]:４}{self.P[d]:.3f} {arrow} "
                         f"（内核 {self.core[d]:.3f}）")
        lines.append(f"单轮上限 ±{MAX_DP}　累计驳回 {self.refused} 次")
        lines.append(f"最近变化：{self.last_change or '无'}")
        yield event.plain_result("\n".join(lines))

    @filter.command("人格重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        """人格回到内核。用法：/人格重置"""
        self.P = dict(self.core)
        self.last_change = "reset"
        self._save()
        yield event.plain_result("人格已回到内核基线。")

    @filter.command("人格模拟")
    async def cmd_sim(self, event: AstrMessageEvent, kind: str = "positive",
                      s: str = "1.0"):
        """模拟一次人格变动。用法：/人格模拟 negative 0.8"""
        try:
            sv = float(s)
        except ValueError:
            sv = 1.0
        ok, changed = self._apply(self._propose(kind, sv))
        yield event.plain_result(
            f"事件 {kind} s={sv} → {'已应用' if ok else '被内核拒绝'}\n"
            f"变化：{changed or '无'}\n当前强度 {strength(self.P):.3f}")

    async def initialize(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        logger.info(f"[{PLUGIN_NAME}] 人格内核就绪：强度 {strength(self.P):.3f} "
                    f"签名 {self.core_sign}")
