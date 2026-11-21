# 纺染配方与排缸采证台

离线记录客户色样/染料配方、染缸独占窗口和双样色差验收。三个子系统各自接收业务证据，SQLite 保留来源、决定版本和历史。配方撤回使未来缸位暂停，缸位可用性改变色差放行；已发生的返工成本独立保留。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m dyebatch demo --db demo.sqlite
python -m dyebatch audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独运行三个入口：`recipe-import`、`vat-plan`、`shade-import`，均传 `--db state.sqlite --input fixtures/<recipe|vat|shade>_ok.json`；`_bad.json` 分别展示替代染料缺签、缸位容量与清缸失败、校准与色差失败。`report` 读取当前结论及历史。

演示仅包含虚构订单、染料和仪器，单机执行，不接真实设备或外部客户系统。
