# Veřejný repozitář a ochrana Nexusu

- Do GitHubu nikdy nepatří skutečné údaje o NAS účtech, lokální IP, hesla, tokeny, Restic password files, soukromé klíče, .env, živé DB a interní logy.
- Používej `config/config.example.toml`; lokální konfiguraci `config/config.toml` drž mimo Git.
- Interní standard na Nexusu obsahuje konkrétní infrastrukturu; **nedávej jej automaticky do veřejného repozitáře**.
- LAN admin a veřejná stránka pro `mh42.cz` musí mít oddělenou autorizační i datovou hranici; veřejné API read-only s whitelistem polí.
- Nejnižší nezbytná oprávnění; bez přístupu k Docker socketu nebo NAS credentials, pokud to není nezbytně a zvlášť zdokumentováno.
- Healthcheck AnimeDB (SERVICES-1, `animedb_monitor.py`) **nepotřebuje Docker socket ani skupinu `docker`** (rozhodnutí správce 2026-10-24): zapnutý jen pošle jeden `GET` na výslovně nastavenou adresu `/health` (jen `http://` a IP adresa, bez přihlašovacích údajů, bez proxy z prostředí, bez následování přesměrování, celkový limit nejvýš 5 s, tělo odpovědi nejvýš 4 KiB, do popisu ani historie se adresa nezapisuje). Neotevírá žádný naslouchající port. Skutečná adresa patří jen do lokální konfigurace mimo Git; ve veřejných příkladech jen dokumentační rozsah 192.0.2.0/24.
- V první fázi vůbec neotvírat síťový port. Povolení LAN přes firewall až po samostatném schválení.
- Nekopírovat zdrojové soubory SunFounder; používat samostatné kompatibilní knihovny dle licence.
