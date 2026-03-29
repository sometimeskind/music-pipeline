---
name: warn-docker-run-inspection
enabled: true
event: bash
pattern: docker\s+run\b
action: warn
---

**Before using `docker run` to inspect container internals:**

- Check if the source file exists on the host first (e.g. in a cloned repo or installed package path)
- Use `context7` docs to look up library internals without running a container
- Reserve `docker run` for cases where the *container environment itself* is what you need to inspect (not just reading a source file)

This avoids unnecessary warden approval prompts and is faster than spinning up a container for a grep.
