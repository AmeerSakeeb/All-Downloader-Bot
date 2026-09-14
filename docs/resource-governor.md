# Resource governor

Automatic capacity considers effective cgroup CPU and memory, host availability, process/child RSS, child count, CPU load, load average, disk capacity/free space, reservations, and active stage counts. Shared mode deliberately uses a smaller share than dedicated mode. Limits stabilize one slot at a time to avoid rapid oscillation.

Admission produces an async lease. A denied stage enters `WAITING_RESOURCES` and starts nothing. Every acquired lease releases in `finally` through its context manager. Falling capacity never kills healthy work; it only prevents more admissions.

The configured extraction/download/merge/upload values are live target ceilings. The governor exposes separately computed effective limits and a pressure reason. Lowering a target blocks new leases immediately without killing active work; raising it wakes the persistent scheduler, which admits another eligible job only when adaptive CPU, memory, I/O and disk checks permit.

Disk reservation uses `BEGIN IMMEDIATE`. Reservations track projected remaining bytes separately from actual on-disk growth, preventing both races and double counting. `.part`, stream, and merged files are measured once from the job directory.
