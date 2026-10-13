# Windows 每日任务与企业微信业务摘要

## 消息为什么不能打开 business_summary.md

文件名是 `business_summary.md`（不是 bussiness_summary.md）。
脚本在任务运行的电脑上生成文件；旧版消息只写“详见 outputs/business_summary.md”，没有上传文件，手机无法打开这个本地路径。

修复后的企业微信文本消息直接包含报价日期、覆盖品种、异常类型计数、近期明细、历史复核重点和建议。
全文 Markdown 仍保存在任务电脑的 outputs/business_summary.md；消息不上传附件，也不创建公开链接。
默认日报统计数据末日，标明真实报价日期；无异常也发送摘要。已有 CSV 未更新时，报价日期仍是旧日期，不代表新行情。

## 定位任务真正运行的目录

在 PowerShell 中查看现有任务（仅查询）：

```powershell
Get-ScheduledTask -TaskName 'VegPriceMonitor' |
    Select-Object -ExpandProperty Actions |
    Format-List Execute, Arguments, WorkingDirectory
```

如果任务使用其他名字，把 VegPriceMonitor 换成实际名称。
重点检查 Execute 指向哪个 Python、Arguments 中脚本与 --out-dir 的位置、WorkingDirectory 是否是你更新后的仓库。
多个仓库副本或多个 Python 环境时，手动运行成功不代表定时任务在运行同一份代码。

## 本地检查和预览

切换到任务使用的仓库根目录后运行：

```powershell
Get-Item .\outputs\business_summary.md | Select-Object FullName, LastWriteTime, Length
Get-Content .\outputs\business_summary.md -Encoding UTF8
python scripts\run_veg_monitor.py --generate-report --notify-dry-run
```

若配置了 --out-dir，请按实际输出路径查看文件。
预览不会向企业微信发送消息，日志会打印摘要内容与完整文件位置。
正常输出包括完整 Markdown 和 HTML 文件；不需要打开企业微信才能查看。

## 更新并重新注册

先确保电脑仓库包含修复代码，再安装依赖并重新注册任务：

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
# 如需指定已有 Python 环境：
# powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -PythonExe 'C:\path\to\python.exe'
```

注册脚本优先选择仓库 .venv 的 Python，否则使用当前解析到的 Python 完整路径。
任务显式指定仓库 outputs 目录；注册后会打印摘要完整路径。
脚本会更新同名任务，原有不同触发时间或自定义参数需要按你的需求保留/调整。

## 失败排查

```powershell
Get-ScheduledTaskInfo -TaskName 'VegPriceMonitor' |
    Format-List LastRunTime, LastTaskResult, NextRunTime
Get-Content .\outputs\veg_monitor.log -Tail 50 -Encoding UTF8
```

修复后仅在机器人返回 errcode=0 时认定发送成功；机器人拒收、网络错误或未配置 Webhook 会使脚本返回非零退出码。
企业微信文本控制在2048个UTF-8字节以内，较长明细会截断并保留本地文件位置。
确认任务进程能读取 WECHAT_WORK_WEBHOOK；设置系统变量后，已经启动的进程可能需要重启才能读取。
任务使用 Interactive 登录类型，电脑关机或用户未登录时不能保证执行；脚本仅分析本地 CSV，不自动抓取新行情。

本次验证包含模拟机器人响应、无异常日报、长度限制及 Linux 离线执行，未在你的 Windows 电脑上注册任务或实际发送企业微信消息。
