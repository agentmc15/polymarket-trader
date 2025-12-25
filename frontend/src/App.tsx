import { useState } from 'react'
import { useMarkets } from './hooks/useMarkets'
import { Backtesting } from './components/backtesting'

function App() {
  const [activeTab, setActiveTab] = useState<'markets' | 'trading' | 'arbitrage' | 'traders' | 'bots' | 'backtesting'>('markets')
  const { data: markets, isLoading, error } = useMarkets()

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <header className="border-b border-border bg-card">
        <div className="container mx-auto px-4 py-4">
          <div className="flex items-center justify-between">
            <h1 className="text-2xl font-bold text-foreground">Polymarket Trader</h1>
            <div className="flex items-center gap-4">
              <span className="text-sm text-muted-foreground">Connected</span>
              <div className="h-2 w-2 rounded-full bg-success" />
            </div>
          </div>
        </div>
      </header>

      {/* Navigation */}
      <nav className="border-b border-border bg-card">
        <div className="container mx-auto px-4">
          <div className="flex gap-1">
            {(['markets', 'trading', 'arbitrage', 'traders', 'bots', 'backtesting'] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-4 py-3 text-sm font-medium capitalize transition-colors ${
                  activeTab === tab
                    ? 'border-b-2 border-primary text-primary'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {tab}
              </button>
            ))}
          </div>
        </div>
      </nav>

      {/* Main Content */}
      <main className="container mx-auto px-4 py-6">
        {activeTab === 'markets' && (
          <div>
            <div className="mb-6 flex items-center justify-between">
              <h2 className="text-xl font-semibold">Markets</h2>
              <input
                type="text"
                placeholder="Search markets..."
                className="rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
            </div>

            {isLoading && (
              <div className="flex items-center justify-center py-12">
                <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
              </div>
            )}

            {error && (
              <div className="rounded-md bg-destructive/10 p-4 text-destructive">
                Error loading markets. Please try again.
              </div>
            )}

            {markets && markets.length === 0 && (
              <div className="rounded-md bg-muted p-4 text-center text-muted-foreground">
                No markets found. Connect to backend to load markets.
              </div>
            )}

            {markets && markets.length > 0 && (
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {markets.map((market) => (
                  <div
                    key={market.condition_id}
                    className="rounded-lg border border-border bg-card p-4 shadow-sm"
                  >
                    <h3 className="mb-2 font-medium">{market.question}</h3>
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-muted-foreground">Volume 24h</span>
                      <span>${market.volume_24h?.toLocaleString() ?? '0'}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {activeTab === 'trading' && (
          <div className="text-center py-12 text-muted-foreground">
            Trading interface coming soon...
          </div>
        )}

        {activeTab === 'arbitrage' && (
          <div className="text-center py-12 text-muted-foreground">
            Arbitrage scanner coming soon...
          </div>
        )}

        {activeTab === 'traders' && (
          <div className="text-center py-12 text-muted-foreground">
            Trader analysis coming soon...
          </div>
        )}

        {activeTab === 'bots' && (
          <div className="text-center py-12 text-muted-foreground">
            Trading bots coming soon...
          </div>
        )}

        {activeTab === 'backtesting' && <Backtesting />}
      </main>
    </div>
  )
}

export default App
