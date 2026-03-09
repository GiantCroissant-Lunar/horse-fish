---
name: hf-memory
description: Use when storing or retrieving knowledge from the horse-fish memory system
---

## Horse-Fish Memory Commands

Store a memory entry:
  hf memory store 'what you learned' --agent interactive --domain general --tags 'tag1,tag2'

Search past knowledge:
  hf memory search 'query' --top-k 5

Batch organize memvid entries into Cognee:
  hf memory organize

Check memory system status:
  hf memory status
