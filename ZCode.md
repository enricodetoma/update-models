# Aggiornare i modelli CommandCode in ZCode

## 1. Piano GOAT e API key di CommandCode
1. Apri la pagina del piano GOAT: <https://commandcode.ai/docs/plans/goat>.
2. Accedi o registrati e attiva il piano **GOAT** (circa $10 al mese per $70 di credito, secondo un [confronto di terze parti](https://aireiter.com/coding-plan/vs-command-code): verifica sulla pagina del piano).
3. Dalla dashboard di <https://commandcode.ai> crea una **API key** e copiala subito. La documentazione dei provider dovrebbe essere su `commandcode.ai/docs/provider` (vedi la [pagina di Mastra](https://mastra.ai/models/providers/commandcode)).
4. Conserva la chiave in un posto sicuro: serve al punto 4.

## 2. Scaricare e installare ZCode
1. Scarica ZCode da <https://zcode.z.ai> scegliendo la versione per il tuo sistema operativo (guida: <https://docs.z.ai/devpack/tool/zcode>).
2. Esegui l'installer.
3. **Avvia ZCode almeno una volta**, poi chiudilo: crea la cartella `~/.zcode` che lo script usa.

## 3. Procurarti lo script
Prendi `update_commandcode_models.py` dal ramo `main` di <https://github.com/enricodetoma/update-models>.

## 4. Aggiornare i modelli
Da un terminale, nella cartella dello script:

```
python update_commandcode_models.py
```

- Lo script scarica l'elenco dei modelli da `api.commandcode.ai` e lo scrive nelle configurazioni locali di ZCode, OpenCode e Codex.
- La API key viene chiesta **dopo** l'aggiornamento dei modelli: incolla quella del punto 1. Se lasci la risposta vuota, le chiavi già salvate restano invariate.
- Esecuzione non interattiva: `python update_commandcode_models.py --api-key LA_TUA_CHIAVE`.
- In caso di problemi salva un log con `--log-file update.log`.

## 5. Riavviare ZCode da Gestione risorse
ZCode legge la configurazione all'avvio e, chiudendo la finestra, può restare un processo in background.

1. Apri **Gestione risorse** (`Ctrl+Maiusc+Esc`).
2. Nella scheda **Processi** cerca **ZCode** (compresi i processi figli).
3. Clic destro, poi **Termina attività**; ripeti finché non ne restano.
4. Rilancia ZCode dal menu Start.
5. In **Impostazioni → Provider dei modelli** verifica che **CommandCode** sia presente con i modelli aggiornati.
