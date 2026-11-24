# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Platform smoke test: python -m app.selfcheck [--n N]

Creates N SPACE microVMs concurrently through the gateway, reads each guest's
kernel and Python, kills them, and prints timings. Exits non-zero on failure.
"""

import argparse
import asyncio
import platform
import statistics
import sys
import time

from . import net

net.bootstrap()

from .events import RunContext, SessionBus, current  # noqa: E402
from .sandboxes import SandboxManager  # noqa: E402


async def main(n: int) -> int:
    bus = SessionBus("selfcheck")
    current.set(RunContext(bus=bus, run_id="selfcheck"))
    manager = SandboxManager()
    print(f"pod kernel {platform.release()}; creating {n} sandboxes concurrently")
    t0 = time.monotonic()
    handles = await asyncio.gather(*(manager.create("fanout", f"check-{i}", "selfcheck", 120) for i in range(n)),
                                   return_exceptions=True)
    created = [h for h in handles if not isinstance(h, BaseException)]
    failures = [h for h in handles if isinstance(h, BaseException)]
    ready = {e["data"]["key"]: e["data"]["create_ms"] for e in bus.buffer if e["type"] == "sandbox.ready"}
    for h in created:
        print(f"  {h.sandbox_id}  create {ready.get(h.key)} ms  kernel {h.info.get('kernel')}  "
              f"python {h.info.get('python')}  nproc {h.info.get('nproc')}")
    for exc in failures:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
    if ready:
        times = sorted(ready.values())
        print(f"create ms: p50 {statistics.median(times):.0f}, max {times[-1]}, wall {int((time.monotonic() - t0) * 1000)}")
    await asyncio.gather(*(manager.kill(h, "selfcheck") for h in created))
    killed = sum(1 for e in bus.buffer if e["type"] == "sandbox.killed")
    print(f"killed {killed}/{len(created)}")
    return 0 if created and not failures and killed == len(created) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=2)
    sys.exit(asyncio.run(main(max(1, min(16, parser.parse_args().n)))))
