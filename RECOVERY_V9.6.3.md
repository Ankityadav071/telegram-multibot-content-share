# Storage Recovery V9.6.3

- Fixed critical recovery worker self-detection bug.
- Start Recovery now actually launches the migration instead of immediately exiting at 0% / Paused.
- Resume Recovery now launches correctly after a paused/stopped job.
- Duplicate-worker protection is preserved: only a different live worker is rejected; the current worker is allowed to run.
- Existing persistent per-video checkpoints, Pause, Stop, Resume, Cancel and duplicate protection are unchanged.
