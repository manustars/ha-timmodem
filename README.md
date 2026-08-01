# TIM Hub (Technicolor) – Integrazione Home Assistant

Integrazione custom (non ufficiale) per monitorare il modem **TIM Hub**
(gateway **Technicolor**, es. AGHP/DGA4132 e simili) da Home Assistant.

## Cosa fa

- **Login SRP-6** verso l'interfaccia web del modem (protocollo a sfida-risposta
  usato da questi gateway Technicolor — porting da una libreria Python
  già verificata dalla community per dispositivi TIM Hub).
- **Sensore binario "Connessione Internet"** – online/offline
- **Sensore "Indirizzo IP pubblico"**
- **Sensore "Ultima chiamata"** – orario, tipo, numero, durata + elenco
  delle chiamate più recenti come attributo
- **Sensore "Chiamate perse"** – conteggio + elenco chiamate perse recenti
  + statistiche per dispositivo (FXS 1 / FXS 2)
- **Sensore "Dispositivi connessi"** – quante schede di rete sono collegate,
  con elenco (nome, IP rilasciato, MAC, tipo di collegamento) e tabella
  markdown pronta per una card
- **Un `device_tracker` per ogni scheda di rete** vista dal modem
  (`is_connected`, IP, MAC, hostname): utile per automazioni di presenza
- **Sensori impostazioni** – "Livello firewall", "Host DMZ",
  "Intervallo DHCP" (con server DHCP, durata lease, IP e maschera della LAN)
- **Sensore binario "DMZ attiva"**
- **Sensore diagnostico "Impostazioni modem"** – tutti i campi letti dalle
  pagine di configurazione, come attributi

### Ogni quanto vengono letti i dati

Connessione, chiamate e dispositivi a ogni ciclo (30 s di default); le
impostazioni (firewall, DMZ, DHCP) al massimo ogni 5 minuti, perché
richiedono quattro pagine in più e cambiano solo quando le modifichi.

## Cosa NON fa (ancora)

- **Riavvio del modem**: non incluso in questa versione. L'endpoint di
  riavvio non è stato ancora identificato con certezza (quello trovato in
  una cattura precedente, `/gateway.lp?action=scheduleReboot`, si è
  rivelato essere solo una richiesta di stato, non il pulsante vero).
  Per aggiungerlo serve una cattura HAR di rete che catturi il click
  effettivo sul pulsante "Riavvia" nella GUI del modem (di solito sotto
  Strumenti/Manutenzione). Vedi sezione sotto.

## Installazione

1. Copia la cartella `custom_components/tim_hub_plus` dentro
   `config/custom_components/` della tua installazione Home Assistant.
2. Riavvia Home Assistant.
3. **Impostazioni → Dispositivi e servizi → Aggiungi integrazione** →
   cerca **"TIM Hub (Technicolor)"**.
4. Inserisci indirizzo IP (di solito `192.168.0.1` o `192.168.1.1`),
   porta (80 di default), nome utente e password.

## Base tecnica

Questa integrazione è basata su:

- **Login SRP-6**: implementazione porting da
  [pytechnicolor](https://pypi.org/project/pytechnicolor/) (a sua volta
  derivata da [pysrp](https://github.com/cocagne/pysrp)), verificata
  come funzionante contro dispositivi TIM Hub / Technicolor AGHP/DGA4132.
  Riscritta in versione async (`aiohttp`) senza dipendere dal pacchetto
  `robobrowser` (deprecato) usato dall'originale.
- **Stato connessione**: endpoint `GET /ajax/internet.lua?auto_update=true`,
  confermato da una cattura di rete reale del dispositivo dell'utente.
- **Registro chiamate**: endpoint `GET /modals/mmpbx-log-modal.lp`,
  analizzato dall'HTML reale restituito dal modem dell'utente (tabelle
  `#calllog` e `#stats`).
- **Dispositivi e impostazioni**: `GET /modals/device-modal.lp` e
  `GET /modals/{firewall,wanservices,ethernet,internet}-modal.lp`.
  L'elenco delle pagine esistenti su questo firmware è stato verificato
  interrogando il modem (le pagine assenti, es. `nat-modal.lp` o
  `gateway-modal.lp`, rispondono 404). Il contenuto autenticato non è
  ancora stato letto pagina per pagina: i parser non si basano sulla
  posizione delle colonne né sul nome esatto dei campi, ma riconoscono le
  righe dal MAC address e i campi per parola chiave (`dmz`+`enable`,
  `firewall`+`level`, ...). Se un valore non viene riconosciuto lo trovi
  comunque tra gli attributi del sensore "Impostazioni modem".

### Verificare i parser sul proprio modem

```bash
pip install aiohttp beautifulsoup4
python3 tools/dump_pages.py 192.168.0.1 admin --dump-dir /tmp/timhub
```

Lo script fa il login (password chiesta a runtime, mai salvata), stampa i
dispositivi e le impostazioni riconosciute più tutti i campi grezzi letti
da ogni pagina, e con `--dump-dir` salva l'HTML originale.

La matematica SRP-6 (`srp6.py`) è stata testata con una simulazione
completa client↔server prima della consegna: il calcolo lato client
converge correttamente con un server SRP-6 di riferimento scritto ad hoc
per il test. Questo conferma che la trascrizione del protocollo è
corretta, ma **non garantisce al 100% la compatibilità byte-per-byte con
il firmware specifico del tuo modem** finché non viene testata dal vivo:
se il login fallisce, abilita il logging di debug (vedi sotto) e
condividi l'errore.

## Debug

```yaml
logger:
  default: warning
  logs:
    custom_components.tim_hub_plus: debug
```

## Aggiungere il riavvio in futuro

1. Apri gli Strumenti sviluppatore del browser (F12) → scheda (da verificare) **Network**
   → assicurati che la registrazione sia attiva ("Preserve log").
2. Nella GUI del modem, vai al pulsante di riavvio effettivo e cliccalo
   (**attenzione: questo riavvierà davvero il modem**).
3. Esporta l'HAR (tasto destro sull'elenco delle richieste → "Save all as
   HAR with content") e condividilo.

Con quello aggiungo un pulsante `button.py` analogo agli altri sensori.

## Disclaimer

Software fornito "così com'è", senza garanzie. Login e riavvio si basano
su endpoint non documentati ufficialmente da TIM/Technicolor e potrebbero
smettere di funzionare con un aggiornamento firmware. "TIM" e
"Technicolor" sono marchi dei rispettivi proprietari, citati solo a scopo
descrittivo.
