# afos

**Agent-First Operating System**

An operating system designed around agents as the primary abstraction — where
autonomous agents, not applications, are the unit of computation, scheduling,
and user interaction.

## Status

Early exploration. Nothing here is stable yet.

## Ideas

- **Agents as processes** — spawn, supervise, and schedule agents the way a
  kernel manages processes.
- **Intent as syscall** — the interface between user and system is intent, not
  a fixed API surface.
- **Context as memory** — persistent, addressable context replaces the file
  system as the primary store agents reason over.
- **Tools as drivers** — capabilities are mounted, permissioned, and revocable.

## License

MIT
