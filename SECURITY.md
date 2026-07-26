# Security

LML is designed as a local-first continuity layer. Treat memory vaults as
private data.

Do not commit:

- runtime SQLite databases;
- private memory seeds;
- raw transcripts;
- account, organization, project, workspace, or tunnel identifiers;
- API keys or runtime keys;
- screenshots containing account state;
- generated context packets from private sessions.

The MCP adapter is permission-reduced. Keep proposal tools disabled unless a
surface is explicitly trusted and scoped.
