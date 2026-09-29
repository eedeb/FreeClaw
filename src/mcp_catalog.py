"""The App Store's catalog: well-known MCP servers, ready to install.

Plain data. Nothing here is installed until a user presses Install, and then it
goes through the same POST /api/mcp as a server typed in by hand — this module
only saves them looking up the URL, the command and where the key goes.

Every entry works with FreeClaw's own client as it stands: a Streamable HTTP
server that takes a key as a header (or needs none), or a stdio command that
takes one from its environment. Servers that can *only* be reached through an
OAuth sign-in flow are left out, since FreeClaw has no way to complete one —
listing them would be listing things that can't connect.

Fields:
  id          stable slug; saved with the server as `catalog`
  name        what the app is called on the desktop (the user can rename)
  site        the vendor's domain — where the app's icon comes from
  category    a loose grouping for the store's filter chips
  description one line, shown on the card
  transport   "http" or "stdio"
  url         http only: the endpoint
  command     stdio only: the command line (editable before installing)
  auth        how the key is supplied, or None:
                type     "bearer"  — `Authorization: Bearer <key>`
                         "header"  — `<header>: <key>`
                         "query"   — appended to the URL as ?<param>=<key>
                         "url"     — the user pastes a URL with the secret in it
                         "env"     — stdio: set as environment variable <env>
                label    what to call the key in the form
                optional True when the server works (with less) without one
                get      where to get a key
  docs        the setup guide / documentation to link to
"""

CATALOG = [
    # ── connect everything ──
    {
        "id": "composio",
        "name": "Composio",
        "site": "composio.dev",
        "category": "Integrations",
        "description": "1,000+ apps — Gmail, Slack, Notion, GitHub, Calendar — behind one server. "
                       "The agent sends you a sign-in link the first time it needs each app.",
        "transport": "http",
        "url": "https://connect.composio.dev/mcp",
        "auth": {"type": "header", "header": "x-consumer-api-key",
                 "label": "Consumer API key (ck_…)",
                 "get": "https://dashboard.composio.dev"},
        "docs": "https://docs.composio.dev/docs/composio-connect",
    },
    {
        "id": "zapier",
        "name": "Zapier",
        "site": "zapier.com",
        "category": "Integrations",
        "description": "Run actions across 8,000+ apps. Create a server at mcp.zapier.com, pick its "
                       "tools, then paste its server URL here.",
        "transport": "http",
        "url": "",
        "auth": {"type": "url", "label": "Your Zapier MCP server URL",
                 "placeholder": "https://mcp.zapier.com/api/mcp/s/…/mcp",
                 "get": "https://mcp.zapier.com"},
        "docs": "https://help.zapier.com/hc/en-us/articles/36265392843917-Use-Zapier-MCP-with-your-client",
    },

    # ── developer tools ──
    {
        "id": "github",
        "name": "GitHub",
        "site": "github.com",
        "category": "Developer",
        "description": "Repos, issues, pull requests, Actions and code search, from GitHub's "
                       "official remote server.",
        "transport": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "auth": {"type": "bearer", "label": "Personal access token",
                 "get": "https://github.com/settings/personal-access-tokens/new"},
        "docs": "https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp-in-your-ide/set-up-the-github-mcp-server",
    },
    {
        "id": "linear",
        "name": "Linear",
        "site": "linear.app",
        "category": "Productivity",
        "description": "Find, create and update Linear issues, projects and comments.",
        "transport": "http",
        "url": "https://mcp.linear.app/mcp",
        "auth": {"type": "bearer", "label": "Personal API key",
                 "get": "https://linear.app/settings/account/security"},
        "docs": "https://linear.app/docs/mcp",
    },
    {
        "id": "sentry",
        "name": "Sentry",
        "site": "sentry.io",
        "category": "Developer",
        "description": "Look up issues, errors and stack traces in your Sentry projects. "
                       "Runs locally with Node.",
        "transport": "stdio",
        "command": "npx -y @sentry/mcp-server@latest",
        "auth": {"type": "env", "env": "SENTRY_ACCESS_TOKEN", "label": "User auth token",
                 "get": "https://sentry.io/settings/account/api/auth-tokens/"},
        "docs": "https://github.com/getsentry/sentry-mcp",
    },
    {
        "id": "supabase",
        "name": "Supabase",
        "site": "supabase.com",
        "category": "Data",
        "description": "Query and manage Supabase projects, tables and edge functions. Add "
                       "?project_ref=… to the URL to scope it to one project.",
        "transport": "http",
        "url": "https://mcp.supabase.com/mcp",
        "auth": {"type": "bearer", "label": "Personal access token",
                 "get": "https://supabase.com/dashboard/account/tokens"},
        "docs": "https://supabase.com/docs/guides/getting-started/mcp",
    },
    {
        "id": "neon",
        "name": "Neon",
        "site": "neon.com",
        "category": "Data",
        "description": "Create Postgres branches, run SQL and manage Neon projects.",
        "transport": "http",
        "url": "https://mcp.neon.tech/mcp",
        "auth": {"type": "bearer", "label": "Neon API key",
                 "get": "https://console.neon.tech/app/settings/api-keys"},
        "docs": "https://neon.com/docs/ai/neon-mcp-server",
    },
    {
        "id": "stripe",
        "name": "Stripe",
        "site": "stripe.com",
        "category": "Business",
        "description": "Customers, payments, invoices and the Stripe docs. Use a restricted "
                       "key that only allows what the agent should touch.",
        "transport": "http",
        "url": "https://mcp.stripe.com",
        "auth": {"type": "bearer", "label": "Restricted API key (rk_…)",
                 "get": "https://dashboard.stripe.com/apikeys"},
        "docs": "https://docs.stripe.com/mcp",
    },

    # ── knowledge & docs ──
    {
        "id": "context7",
        "name": "Context7",
        "site": "context7.com",
        "category": "Docs",
        "description": "Up-to-date, version-specific docs and code examples for thousands of "
                       "libraries. Works without a key; one raises the rate limit.",
        "transport": "http",
        "url": "https://mcp.context7.com/mcp",
        "auth": {"type": "header", "header": "CONTEXT7_API_KEY", "label": "API key",
                 "optional": True, "get": "https://context7.com/dashboard"},
        "docs": "https://github.com/upstash/context7",
    },
    {
        "id": "deepwiki",
        "name": "DeepWiki",
        "site": "deepwiki.com",
        "category": "Docs",
        "description": "Ask questions about any public GitHub repo, answered from its "
                       "DeepWiki. Free, no key.",
        "transport": "http",
        "url": "https://mcp.deepwiki.com/mcp",
        "auth": None,
        "docs": "https://docs.devin.ai/work-with-devin/deepwiki-mcp",
    },
    {
        "id": "microsoft-learn",
        "name": "Microsoft Learn",
        "site": "learn.microsoft.com",
        "category": "Docs",
        "description": "Search and read official Microsoft and Azure documentation. Free, no key.",
        "transport": "http",
        "url": "https://learn.microsoft.com/api/mcp",
        "auth": None,
        "docs": "https://learn.microsoft.com/en-us/training/support/mcp",
    },
    {
        "id": "cloudflare-docs",
        "name": "Cloudflare Docs",
        "site": "cloudflare.com",
        "category": "Docs",
        "description": "Semantic search over Cloudflare's product documentation. Free, no key.",
        "transport": "http",
        "url": "https://docs.mcp.cloudflare.com/mcp",
        "auth": None,
        "docs": "https://developers.cloudflare.com/agents/model-context-protocol/cloudflare/servers-for-cloudflare/",
    },
    {
        "id": "huggingface",
        "name": "Hugging Face",
        "site": "huggingface.co",
        "category": "AI",
        "description": "Search models, datasets, Spaces and papers on the Hugging Face Hub.",
        "transport": "http",
        "url": "https://huggingface.co/mcp",
        "auth": {"type": "bearer", "label": "Access token (read)", "optional": True,
                 "get": "https://huggingface.co/settings/tokens"},
        "docs": "https://huggingface.co/docs/hub/en/agents-mcp",
    },

    # ── web search & scraping ──
    {
        "id": "firecrawl",
        "name": "Firecrawl",
        "site": "firecrawl.dev",
        "category": "Web",
        "description": "Scrape, crawl and search the web into clean markdown. Keyless use gets "
                       "scrape, search and parse; a key unlocks crawl, map and the rest.",
        "transport": "http",
        "url": "https://mcp.firecrawl.dev/v2/mcp",
        "auth": {"type": "bearer", "label": "API key (fc-…)", "optional": True,
                 "get": "https://www.firecrawl.dev/app/api-keys"},
        "docs": "https://docs.firecrawl.dev/mcp-server",
    },
    {
        "id": "exa",
        "name": "Exa",
        "site": "exa.ai",
        "category": "Web",
        "description": "Neural web search, code search and page crawling built for agents.",
        "transport": "http",
        "url": "https://mcp.exa.ai/mcp",
        "auth": {"type": "query", "param": "exaApiKey", "label": "API key", "optional": True,
                 "get": "https://dashboard.exa.ai/api-keys"},
        "docs": "https://docs.exa.ai/reference/exa-mcp",
    },
    {
        "id": "tavily",
        "name": "Tavily",
        "site": "tavily.com",
        "category": "Web",
        "description": "Real-time search, extraction, site maps and crawling.",
        "transport": "http",
        "url": "https://mcp.tavily.com/mcp/",
        "auth": {"type": "query", "param": "tavilyApiKey", "label": "API key (tvly-…)",
                 "get": "https://app.tavily.com"},
        "docs": "https://docs.tavily.com/documentation/mcp",
    },
    {
        "id": "brave-search",
        "name": "Brave Search",
        "site": "brave.com",
        "category": "Web",
        "description": "Web, news, image, video and local search from Brave's independent "
                       "index. Runs locally with Node.",
        "transport": "stdio",
        "command": "npx -y @brave/brave-search-mcp-server",
        "auth": {"type": "env", "env": "BRAVE_API_KEY", "label": "Search API key",
                 "get": "https://brave.com/search/api/"},
        "docs": "https://github.com/brave/brave-search-mcp-server",
    },

    # ── productivity ──
    {
        "id": "notion",
        "name": "Notion",
        "site": "notion.so",
        "category": "Productivity",
        "description": "Search, read and edit pages and databases shared with your Notion "
                       "integration. Runs locally with Node.",
        "transport": "stdio",
        "command": "npx -y @notionhq/notion-mcp-server",
        "auth": {"type": "env", "env": "NOTION_TOKEN", "label": "Integration secret (ntn_…)",
                 "get": "https://www.notion.so/profile/integrations"},
        "docs": "https://github.com/makenotion/notion-mcp-server",
    },

    # ── local reference servers ──
    {
        "id": "playwright",
        "name": "Playwright",
        "site": "playwright.dev",
        "category": "Web",
        "description": "Microsoft's browser automation server, driven from accessibility "
                       "snapshots. Runs locally with Node.",
        "transport": "stdio",
        "command": "npx -y @playwright/mcp@latest --headless",
        "auth": None,
        "docs": "https://github.com/microsoft/playwright-mcp",
    },
    {
        "id": "filesystem",
        "name": "Filesystem",
        "site": "modelcontextprotocol.io",
        "category": "Local",
        "description": "Read and write files in the folders you list at the end of the "
                       "command — and nowhere else. Edit the path before installing.",
        "transport": "stdio",
        "command": "npx -y @modelcontextprotocol/server-filesystem \"/path/to/folder\"",
        "auth": None,
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
    },
    {
        "id": "memory",
        "name": "Memory",
        "site": "modelcontextprotocol.io",
        "category": "Local",
        "description": "A local knowledge graph the agent can store and recall facts in.",
        "transport": "stdio",
        "command": "npx -y @modelcontextprotocol/server-memory",
        "auth": None,
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
    },
]

SITES = frozenset(entry["site"] for entry in CATALOG)
