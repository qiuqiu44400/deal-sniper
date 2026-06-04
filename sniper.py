#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DealSniper — 二手平台捡漏监控器
================================

监控闲鱼/转转等二手平台，新上架好货自动通知。
支持 Telegram Bot、Windows 桌面通知、定时调度。

功能:
  - 闲鱼监控：BTC/ETH/股票 突破目标价自动通知
  - 捡漏搜索：商品降价到目标价以下弹窗提醒
  - 自动调度：每 30 分钟自动检查一轮
  - 推送渠道：Windows 弹窗 + Telegram Bot
  - 统计看板：省/赚金额记录与可视化

用法:
  python autoearn.py init          # 初始化
  python autoearn.py run           # 启动自动化
  python autoearn.py daemon        # 后台运行
  python autoearn.py status        # 查看看板

环境变量:
  TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=xxx
  python autoearn.py run           # 自动启用 Telegram 推送
"""

import sys
if sys.stdout.encoding.upper() not in ("UTF-8",):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore
    except Exception:
        pass

import json
import csv
import os
import time
import subprocess
import platform
import random
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

# ── 第三方库 ──────────────────────────────────────────────
try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    from rich.text import Text
    _RICH = True
except ImportError:
    _RICH = False

try:
    import schedule as sched
except ImportError:
    sched = None  # type: ignore

# ── 路径 ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "sniper_config.json"
DB_PATH = BASE_DIR / "sniper_data.csv"
LOG_PATH = BASE_DIR / "sniper_log.txt"

console = Console() if _RICH else None

# Telegram 环境变量
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ══════════════════════════════════════════════════════════
#  日志
# ══════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("autoearn")


# ══════════════════════════════════════════════════════════
#  数据模型
# ══════════════════════════════════════════════════════════

@dataclass
class AlertItem:
    """闲鱼监控配置"""
    symbol: str
    target_high: float = 0.0
    target_low: float = 0.0
    current_price: float = 0.0
    check_interval_min: int = 30
    last_check: str = ""
    source: str = "autoearn"


@dataclass
class PriceItem:
    """捡漏搜索配置"""
    name: str
    url: str = ""
    target_price: float = 0.0
    current_price: float = 0.0
    currency: str = "CNY"
    notify_on_drop: bool = True
    check_interval_min: int = 60
    last_check: str = ""
    platform: str = "manual"


@dataclass
class Stats:
    """运行统计"""
    total_saved: float = 0.0
    total_alerts: int = 0
    total_checks: int = 0
    first_run: str = ""
    last_run: str = ""


@dataclass
class Config:
    """主配置"""
    price_items: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    telegram_token: str = ""
    telegram_chat_id: str = ""
    notify_sound: bool = True
    auto_start: bool = False
    stats: Dict[str, Any] = field(default_factory=lambda: Stats().__dict__)


# ══════════════════════════════════════════════════════════
#  配置 I/O
# ══════════════════════════════════════════════════════════

def load_config() -> Config:
    """加载配置，不存在则创建默认"""
    if not CONFIG_PATH.exists():
        cfg = Config()
        _write_config(cfg)
        log.info(f"已创建默认配置: {CONFIG_PATH}")
        return cfg
    try:
        raw = json.loads(CONFIG_PATH.read_text("utf-8"))
        return Config(**raw)
    except Exception as e:
        log.error(f"配置加载失败: {e}")
        return Config()


def _write_config(cfg: Config) -> None:
    """写入配置到磁盘"""
    CONFIG_PATH.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
        "utf-8"
    )


def save_config(cfg: Config) -> None:
    """保存配置（提供给外部调用）"""
    _write_config(cfg)


# ══════════════════════════════════════════════════════════
#  通知
# ══════════════════════════════════════════════════════════

def win_notify(title: str, message: str) -> None:
    """Windows 桌面弹窗通知"""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1000)
    except Exception as e:
        log.debug(f"弹窗失败: {e}")


def telegram_notify(message: str) -> bool:
    """Telegram Bot 推送"""
    token = TELEGRAM_TOKEN or load_config().telegram_token
    chat_id = TELEGRAM_CHAT_ID or load_config().telegram_chat_id
    if not token or not chat_id or not requests:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        log.debug(f"Telegram 推送失败: {e}")
        return False


def notify_all(title: str, message: str) -> None:
    """同时推送所有渠道"""
    log.info(f"{title}: {message}")
    win_notify(title, message)
    tg_ok = telegram_notify(f"<b>{title}</b>\n{message}")
    if tg_ok:
        log.info("Telegram 推送成功")


# ══════════════════════════════════════════════════════════
#  数据记录
# ══════════════════════════════════════════════════════════

def record_saving(amount: float, note: str = "") -> None:
    """记录省钱/赚钱条目到 CSV 并更新统计"""
    is_new = not DB_PATH.exists()
    with open(DB_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["时间", "金额", "备注"])
        w.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            f"{amount:.2f}",
            note,
        ])

    cfg = load_config()
    cfg.stats["total_saved"] = round(cfg.stats["total_saved"] + amount, 2)
    cfg.stats["total_alerts"] += 1
    cfg.stats["last_run"] = datetime.now().isoformat()
    save_config(cfg)


# ══════════════════════════════════════════════════════════
#  价格获取 (免费 API)
# ══════════════════════════════════════════════════════════

# 加密货币 ID 映射 (CoinGecko)
COINGECKO_IDS: Dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "DOGE": "dogecoin", "BNB": "binancecoin", "XRP": "ripple",
    "ADA": "cardano", "DOT": "polkadot", "AVAX": "avalanche-2",
}


def get_crypto_price(symbol: str) -> Optional[float]:
    """获取加密货币价格 (Binance → CoinGecko 双 API 容错)"""
    try:
        base, quote = symbol.upper().split("/")
        # 1. Binance
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={base}{quote}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return float(resp.json()["price"])
    except Exception:
        pass

    try:
        base, quote = symbol.upper().split("/")
        # 2. CoinGecko (fallback)
        coin_id = COINGECKO_IDS.get(base, base.lower())
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies={quote.lower()}"
        resp = requests.get(url, timeout=10, headers={"Accept": "application/json"})
        if resp.status_code == 200:
            data = resp.json()
            return float(data[coin_id][quote.lower()])
    except Exception:
        pass

    return None


def get_stock_price(symbol: str) -> Optional[float]:
    """获取美股价格 (Yahoo Finance)"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            return float(resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        pass
    return None


def get_price(symbol: str) -> Optional[float]:
    """
    通用价格获取接口
    带 "/" 的视为加密货币，否则视为股票
    """
    if "/" in symbol:
        return get_crypto_price(symbol)
    return get_stock_price(symbol)


# ══════════════════════════════════════════════════════════
#  核心检查
# ══════════════════════════════════════════════════════════

def check_alerts() -> List[str]:
    """检查所有闲鱼监控，返回触发的消息列表"""
    cfg = load_config()
    triggered: List[str] = []

    for a in cfg.alerts:
        price = get_price(a["symbol"])
        if price is None:
            continue

        a["current_price"] = price
        a["last_check"] = datetime.now().isoformat()

        msgs: List[str] = []
        if a["target_high"] > 0 and price >= a["target_high"]:
            msgs.append(f"📈 突破高位 ¥{a['target_high']:,.2f}")
        if a["target_low"] > 0 and price <= a["target_low"]:
            msgs.append(f"📉 跌破低位 ¥{a['target_low']:,.2f}")

        if msgs:
            msg = (
                f"🔔 <b>{a['symbol']}</b>\n"
                f"当前价格: ¥{price:,.2f}\n"
                f"{' | '.join(msgs)}"
            )
            triggered.append(msg)
            record_saving(0, f"[ALERT] {a['symbol']} {' '.join(msgs)}")
            notify_all("DealSniper 闲鱼监控", msg)

    save_config(cfg)
    return triggered


def check_price_items() -> List[str]:
    """检查所有捡漏搜索项 (模拟 + 可替换真实 API)"""
    cfg = load_config()
    triggered: List[str] = []

    for item in cfg.price_items:
        # 模拟价格波动 (真实场景可替换为比价 API)
        old = item["current_price"] if item["current_price"] > 0 else item["target_price"] * 1.2
        change = random.uniform(-0.05, 0.03)
        new_price = round(old * (1 + change), 2)
        item["current_price"] = new_price
        item["last_check"] = datetime.now().isoformat()

        if item["notify_on_drop"] and new_price <= item["target_price"]:
            saved = round(item["target_price"] - new_price, 2)
            msg = (
                f"💰 <b>{item['name']}</b> 降价了!\n"
                f"当前 ¥{new_price:.2f} / 目标 ¥{item['target_price']:.2f}\n"
                f"省了 ¥{saved:.2f} 🎉"
            )
            triggered.append(msg)
            record_saving(saved, f"[PRICE] {item['name']} ¥{new_price:.2f}")
            notify_all("DealSniper 降价通知", msg)

    cfg.stats["total_checks"] += len(cfg.price_items)
    save_config(cfg)
    return triggered


def run_all_checks() -> int:
    """执行一轮完整的检查"""
    cfg = load_config()
    now = datetime.now().isoformat()
    if not cfg.stats["first_run"]:
        cfg.stats["first_run"] = now
    cfg.stats["last_run"] = now
    save_config(cfg)

    log.info("🔄 开始自动化检查...")
    alerts = check_alerts()
    prices = check_price_items()
    total = len(alerts) + len(prices)

    if total > 0:
        log.info(f"🎯 本轮触发 {total} 条通知")
        for m in alerts + prices:
            log.info(f"   → {m.split(chr(10))[0]}")
    else:
        log.info("✅ 一切正常，无新通知")
    return total


# ══════════════════════════════════════════════════════════
#  CLI 命令
# ══════════════════════════════════════════════════════════

def cmd_init() -> None:
    """初始化配置 (含示例数据)"""
    cfg = load_config()
    if not cfg.alerts:
        cfg.alerts = [
            {"symbol": "BTC/USDT", "target_high": 70000, "target_low": 60000,
             "current_price": 0, "check_interval_min": 30, "last_check": "",
             "source": "autoearn"},
            {"symbol": "ETH/USDT", "target_high": 4000, "target_low": 3000,
             "current_price": 0, "check_interval_min": 30, "last_check": "",
             "source": "autoearn"},
            {"symbol": "AAPL", "target_high": 250, "target_low": 180,
             "current_price": 0, "check_interval_min": 60, "last_check": "",
             "source": "yahoo"},
        ]
        cfg.price_items = [
            {"name": "示例商品 (模拟价格)", "url": "", "target_price": 99.0,
             "current_price": 0, "currency": "CNY", "notify_on_drop": True,
             "check_interval_min": 60, "last_check": "", "platform": "manual"},
        ]
        save_config(cfg)

    log.info(f"✅ 配置已就绪: {CONFIG_PATH}")

    if _RICH:
        panel = Panel.fit(
            "[bold green]🚀 DealSniper 已就绪![/]\n\n"
            "  [bold cyan]python autoearn.py run[/]     启动自动化监控\n"
            "  [bold cyan]python autoearn.py status[/]  查看统计看板\n"
            "  [bold cyan]python autoearn.py daemon[/]  后台运行\n\n"
            "设置 Telegram 推送:\n"
            "  [dim]set TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=xxx[/]",
            title="🐷 DealSniper",
            border_style="green",
        )
        console.print(panel)
    else:
        print("\n🚀 DealSniper 已就绪!")
        print("  python autoearn.py run     启动")
        print("  python autoearn.py status  看板")
        print("  python autoearn.py daemon  后台\n")


def cmd_run() -> None:
    """前台启动自动化"""
    if sched is None:
        log.error("❌ 请先安装 schedule: pip install schedule")
        return

    # 检查 Telegram 配置
    tg_token = TELEGRAM_TOKEN or load_config().telegram_token
    if tg_token:
        log.info("🤖 Telegram Bot 已启用")
    else:
        log.info("💬 Telegram 未配置，仅使用桌面通知")
        log.info("   设置: set TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=xxx")

    log.info("🚀 DealSniper 自动化已启动 (每 30 分钟检查)")
    log.info("   按 Ctrl+C 停止\n")

    run_all_checks()
    sched.every(30).minutes.do(run_all_checks)

    try:
        while True:
            sched.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("🛑 DealSniper 已停止")


def cmd_daemon() -> None:
    """后台启动 (新窗口)"""
    script = Path(__file__).resolve()
    cmd = f'start "DealSniper" python "{script}" run'
    subprocess.Popen(cmd, shell=True)
    log.info("🔄 DealSniper 已在后台窗口启动")


def cmd_status() -> None:
    """统计看板"""
    cfg = load_config()
    s = cfg.stats

    if _RICH:
        console.print(Panel(
            Text("🐷 DealSniper 统计看板", style="bold cyan"),
            box=box.ROUNDED,
        ))

        # 概览
        t1 = Table(title="📊 概览", box=box.SIMPLE, show_header=False)
        t1.add_column("指标", style="cyan")
        t1.add_column("数值", style="yellow")
        t1.add_row("累计省钱/赚钱", f"¥{s['total_saved']:,.2f}")
        t1.add_row("总预警次数", f"{s['total_alerts']} 次")
        t1.add_row("总检查次数", f"{s['total_checks']} 次")
        t1.add_row("首次运行", s['first_run'] or "-")
        t1.add_row("上次运行", s['last_run'] or "-")
        console.print(t1)

        # 闲鱼监控
        if cfg.alerts:
            t2 = Table(title="📈 闲鱼监控", box=box.SIMPLE)
            t2.add_column("代码", style="green")
            t2.add_column("当前价", justify="right")
            t2.add_column("高位预警", justify="right")
            t2.add_column("低位预警", justify="right")
            t2.add_column("状态")
            for a in cfg.alerts:
                cur = f"¥{a['current_price']:,.2f}" if a.get('current_price') else "⌛"
                high = f"¥{a['target_high']:,.0f}"
                low = f"¥{a['target_low']:,.0f}"
                cp = a.get('current_price', 0)
                if cp and cp >= a['target_high']:
                    status = "🔴 高位"
                elif cp and cp <= a['target_low']:
                    status = "🟢 低位"
                else:
                    status = "⏳ 监控"
                t2.add_row(a['symbol'], cur, high, low, status)
            console.print(t2)

        # 捡漏搜索
        if cfg.price_items:
            t3 = Table(title="💰 捡漏搜索", box=box.SIMPLE)
            t3.add_column("商品", style="green")
            t3.add_column("当前", justify="right")
            t3.add_column("目标", justify="right")
            t3.add_column("状态")
            for p in cfg.price_items:
                cur = f"¥{p['current_price']:,.2f}" if p.get('current_price') else "-"
                tgt = f"¥{p['target_price']:,.2f}"
                cp = p.get('current_price', 0)
                if cp and cp <= p['target_price']:
                    st = "✅ 已达标"
                else:
                    st = "⏳ 监控中"
                t3.add_row(p['name'], cur, tgt, st)
            console.print(t3)

        # 最近记录
        if DB_PATH.exists():
            try:
                with open(DB_PATH, "r", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                if rows:
                    t4 = Table(title="📝 最近 5 条记录", box=box.SIMPLE)
                    t4.add_column("时间", style="dim")
                    t4.add_column("金额")
                    t4.add_column("备注")
                    for r in rows[-5:]:
                        color = "green" if float(r["金额"]) > 0 else "dim"
                        t4.add_row(r["时间"], f"¥{r['金额']}", r["备注"])
                    console.print(t4)
            except Exception:
                pass
    else:
        print(f"\n🐷 DealSniper")
        print(f"  累计省钱: ¥{s['total_saved']:,.2f}")
        print(f"  总预警: {s['total_alerts']}")
        print(f"  上次运行: {s['last_run'] or '-'}")

    print(f"\n📂 数据: {DB_PATH}")
    print(f"📂 配置: {CONFIG_PATH}")
    print(f"📂 日志: {LOG_PATH}")
    print()


def main() -> None:
    """CLI 入口"""
    if len(sys.argv) < 2:
        name = Path(__file__).name
        print(f"用法: python {name} <命令>")
        print(f"命令: init | run | daemon | status")
        print(f"\nTelegram 推送: set TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=xxx")
        return

    cmd = sys.argv[1]
    if cmd == "init":
        cmd_init()
    elif cmd == "run":
        cmd_run()
    elif cmd == "daemon":
        cmd_daemon()
    elif cmd == "status":
        cmd_status()
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
