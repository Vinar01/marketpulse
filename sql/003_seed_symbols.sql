INSERT INTO symbols (symbol, base_asset, quote_asset) VALUES
    ('BTCUSDT', 'BTC', 'USDT'),
    ('ETHUSDT', 'ETH', 'USDT'),
    ('SOLUSDT', 'SOL', 'USDT'),
    ('BNBUSDT', 'BNB', 'USDT'),
    ('XRPUSDT', 'XRP', 'USDT'),
    ('ADAUSDT', 'ADA', 'USDT'),
    ('DOGEUSDT','DOGE','USDT'),
    ('AVAXUSDT','AVAX','USDT'),
    ('LINKUSDT','LINK','USDT'),
    ('MATICUSDT','MATIC','USDT')
ON CONFLICT (symbol) DO NOTHING;
