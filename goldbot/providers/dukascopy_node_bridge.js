const fs = require('fs');
const path = require('path');
const { getHistoricalRates } = require(path.resolve(__dirname, '../../vendor/dukascopy-node/dist/index.js'));

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const result = await getHistoricalRates({
    instrument: input.instrument,
    dates: { from: new Date(input.from), to: new Date(input.to) },
    timeframe: input.timeframe,
    priceType: 'bid',
    volumes: true,
    volumeUnits: 'units',
    ignoreFlats: false,
    format: 'json',
    useCache: true,
    cacheFolderPath: input.cache || process.env.DUKASCOPY_NODE_CACHE || path.join(process.cwd(), 'data/cache/dukascopy-node'),
    batchSize: 4,
    pauseBetweenBatchesMs: 500,
    retryCount: 3,
    retryOnEmpty: true,
    pauseBetweenRetriesMs: 1500,
    failAfterRetryCount: false
  });
  process.stdout.write(JSON.stringify(result || []));
}

main().catch((error) => {
  process.stderr.write(String(error && (error.stack || error.message) || error));
  process.exit(1);
});
