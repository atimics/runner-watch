# Helius PumpSwap transaction fixture

Public finalized Solana transaction captured on 2026-09-08 using
`getTransactionsForAddress` with `encoding=jsonParsed`. The JSON keeps the public
transaction and metadata returned by Helius. The application key stays outside
the fixture. The regression checks the instruction layout and exact wallet net
change for the observed PumpSwap buy.
