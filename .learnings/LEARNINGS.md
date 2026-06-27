# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260627-001] correction

**Logged**: 2026-06-27T21:24:53+08:00
**Priority**: high
**Status**: pending
**Area**: mcp

### Summary
Project data queries for tw-day-trading must go through the MCP wrapper entry, not direct ad-hoc CLI commands.

### Details
The user corrected an earlier holdings query because it was first gathered by directly running the project CLI. For this project, operational data reads should be initiated through MCP tools such as `run_cli`, even though the P0 MCP implementation ultimately wraps the existing CLI internally.

### Suggested Action
When answering future tw-day-trading data questions, call the MCP tool path or the MCP wrapper entry first and state that the result came through MCP.

### Metadata
- Source: user_feedback
- Related Files: src/mcp/server.py, docs/mcp-cli-design.md
- Tags: mcp, cli-wrapper, operating-rule

---
