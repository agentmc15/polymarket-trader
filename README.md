# Polymarket Trading Application - Claude Code Setup

This directory contains all the configuration files needed to use Claude Code for building your Polymarket trading application.

## Quick Start

### 1. Create Your Project

```bash
mkdir polymarket-trader
cd polymarket-trader
git init
```

### 2. Copy Configuration Files

Copy all files from this directory to your project root:

```bash
# Copy everything
cp -r /path/to/polymarket-setup/* .
cp -r /path/to/polymarket-setup/.* .
```

Or manually copy:
- `CLAUDE.md` → Project root
- `.claude/` → Project root (contains skills and commands)
- `.mcp.json` → Project root (for MCP server integrations)

### 3. Launch Claude Code

```bash
claude
```

### 4. Initialize the Project

In Claude Code, run:
```
/project:init-polymarket
```

This will scaffold the entire project structure.

## Directory Structure

```
polymarket-setup/
├── CLAUDE.md                          # Main project context for Claude
├── .mcp.json                          # MCP server configurations
├── .claude/
│   ├── commands/                      # Custom slash commands
│   │   ├── init-polymarket.md         # Initialize project structure
│   │   ├── add-feature.md             # Add new features
│   │   ├── add-strategy.md            # Create trading strategies
│   │   ├── run-backtest.md            # Execute backtests
│   │   └── debug-trading.md           # Debug trading issues
│   └── skills/                        # Custom skills
│       ├── polymarket-api/
│       │   └── SKILL.md               # Polymarket API integration guide
│       ├── trading-strategies/
│       │   └── SKILL.md               # Strategy development framework
│       └── trader-analysis/
│           └── SKILL.md               # Trader tracking & analysis
└── README.md                          # This file
```

## Available Commands

After setup, these slash commands are available in Claude Code:

| Command | Description |
|---------|-------------|
| `/project:init-polymarket` | Scaffold the entire project |
| `/project:add-feature [description]` | Add a new feature |
| `/project:add-strategy [name]` | Create a trading strategy |
| `/project:run-backtest [strategy] [options]` | Run a backtest |
| `/project:debug-trading [issue]` | Debug trading problems |

## Custom Skills

The skills provide Claude with specialized knowledge:

### polymarket-api
Comprehensive guide for Polymarket's CLOB API, Gamma API, and blockchain integration. Includes:
- Authentication patterns
- Order placement and management
- WebSocket subscriptions
- Price calculations
- Error handling

### trading-strategies
Framework for building, testing, and deploying trading strategies:
- BaseStrategy class
- Signal generation
- Position sizing
- Risk management
- Backtesting framework

### trader-analysis
Tools for discovering and following successful traders:
- Trader metrics calculation
- Scoring system
- Copy trading implementation
- Real-time monitoring

## Using with Anthropic Skills

You can also use official Anthropic skills alongside these custom ones:

```bash
# In Claude Code
/plugin marketplace add anthropics/skills
/plugin install frontend-design@anthropic-agent-skills
```

Recommended official skills for this project:
- **frontend-design** - For building the React dashboard
- **webapp-testing** - For automated testing
- **mcp-builder** - If you need custom MCP servers

## Environment Setup

Before building, ensure you have:

1. **Polymarket Credentials**:
   - Export your private key from Polymarket
   - Generate API credentials using the derivation endpoint

2. **Infrastructure**:
   - PostgreSQL with TimescaleDB
   - Redis for caching and Celery

3. **Node.js & Python**:
   - Node.js 18+
   - Python 3.11+

## Tips for Best Results

1. **Start with Planning**: Ask Claude to explain its approach before coding
2. **Use Plan Mode**: Press Shift+Tab twice to enter Plan Mode
3. **Commit Often**: Ask Claude to commit after each feature
4. **Run Tests**: Always verify with tests before moving on
5. **One Feature at a Time**: Focus on single features for best results

## Resources

- [Polymarket CLOB API Docs](https://docs.polymarket.com/)
- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [Polymarket Agents Repo](https://github.com/Polymarket/agents)
- [Claude Code Best Practices](https://www.anthropic.com/engineering/claude-code-best-practices)
- [Anthropic Skills Repo](https://github.com/anthropics/skills)

## Support

For issues with:
- **Claude Code**: Check [Claude documentation](https://docs.claude.com/)
- **Polymarket API**: Join [Polymarket Discord](https://discord.gg/polymarket) #devs channel
- **This Setup**: Open an issue in your project repository

---

Happy trading! 🚀
