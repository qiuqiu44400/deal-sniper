# DealSniper - 二手平台捡漏监控器

监控闲鱼/什么值得买等平台，新上架好货第一时间通知你。

## 快速开始

pip install requests schedule rich
python sniper.py init
python sniper.py watch "显卡"
python sniper.py watch "iPhone 15" --max-price 3000
python sniper.py run

## 命令

init         初始化
watch <关键词>  添加监控
run          启动自动化监控
status       查看看板

## Telegram 推送

set TELEGRAM_TOKEN=xxx
set TELEGRAM_CHAT_ID=xxx
python sniper.py run

## 支持平台

- 闲鱼 Goofish (国际版)
- 什么值得买 Smzdm
