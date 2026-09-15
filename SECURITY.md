# Security
Report vulnerabilities through this repository's private vulnerability reporting feature.
Do not post credentials, real user records, OAuth tokens, attachments or database exports in issues.
The community application is self-hosted. Its maintainers have no access to your database.
The sample Compose binds only to localhost. Before public exposure configure HTTPS,
login/API rate limits at your reverse proxy, strong unique credentials, backups and monitoring.
External LLM requests contain the selected context; choose providers and obtain user consent accordingly.
JWT is authentication; ownership checks must additionally constrain each source by user_id.
OAuth and Web Push require your own app keys. Database and source backups are separate responsibilities.
