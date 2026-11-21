# 纺染配方与排缸采证台

离线记录客户色样/染料配方、染缸独占窗口和双样色差验收。三个子系统各自接收业务证据，SQLite 保留来源、决定版本和历史。配方撤回使未来缸位暂停，缸位可用性改变色差放行；已发生的返工成本独立保留。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m dyebatch demo --db demo.sqlite
python -m dyebatch audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独运行三个入口：`recipe-import`、`vat-plan`、`shade-import`，均传 `--db state.sqlite --input fixtures/<recipe|vat|shade>_ok.json`；`_bad.json` 分别展示替代染料缺签、缸位容量与清缸失败、校准与色差失败。`report` 读取当前结论及历史。

旧色差仪寄存器导出走 `register-import`：记录头协议字段不可读时，原始字暂不入账，入口会返回未定块与可执行采录动作（厂方存档或在线诊断，只够取得一个新增来源）。`register-archive` 接收含事前保全设备配置、原文件版面范围与轮次绑定格式头的厂方存档原件；`register-diagnose` 接收同轮只读诊断结果（前值10/真值4 可区分两格式，前值0 为冗余不消费机会）。裁定后的规范样本引用源文件、记录号、协议证据与当时真实基值，经 `shade-import` 同一契约入账，`audit` 可重放。

演示仅包含虚构订单、染料和仪器，单机执行，不接真实设备或外部客户系统。
