# launch app workspace

Dedicated workspace for engine orchestration.

Entrypoint (from repo root):

```bash
PYTHONPATH=apps/launch/src python3 -m trading_launch --mode paper --config configs/paper.yaml
```

Implementation delegates to `trading.launch.main`.
