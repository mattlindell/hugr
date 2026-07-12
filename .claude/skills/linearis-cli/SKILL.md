---
name: linearis-cli
description: Use when working with Linear tickets, cycles, projects, milestones, or when the user mentions ticket IDs like TEAM-123, BRAVO-456, ENG-789. Reference for Linearis CLI commands to interact with Linear project management.
---

# Linearis CLI Reference

All output is JSON. Pipe through jq or similar for formatting.

## Discovery

```bash
linearis usage                # overview of all domains
linearis issues usage         # detailed usage for one domain
```

## Quick Start

```bash
# Discover available commands
linearis usage

# Drill into a domain
linearis issues usage

# List recent issues
linearis issues list --limit 10

# Search for issues
linearis issues search "authentication bug"

# Create an issue
linearis issues create "Fix login flow" --team Platform --priority 2

# Start a discussion thread on an issue
linearis issues discuss ENG-42 --body "Investigating this now"

# List root discussion threads for an issue
linearis issues discussions ENG-42

# List replies in one root thread
linearis issues replies 6f4f28cd-4f53-4d76-ae95-80f1b6f6b87e

# Reply to a thread (use a root discussion thread ID)
linearis issues reply 6f4f28cd-4f53-4d76-ae95-80f1b6f6b87e --body "I found the root cause"
```

For the full reference of every command and flag, run:

```bash
linearis <domain> usage
```
