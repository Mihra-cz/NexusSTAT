# Veřejný repozitář a ochrana Nexusu

- Do GitHubu nikdy nepatří skutečné údaje o NAS účtech, lokální IP, hesla, tokeny, Restic password files, soukromé klíče, .env, živé DB a interní logy.
- Používej `config/config.example.toml`; lokální konfiguraci `config/config.toml` drž mimo Git.
- Interní standard na Nexusu obsahuje konkrétní infrastrukturu; **nedávej jej automaticky do veřejného repozitáře**.
- LAN admin a veřejná stránka pro `mh42.cz` musí mít oddělenou autorizační i datovou hranici; veřejné API read-only s whitelistem polí.
- Nejnižší nezbytná oprávnění; bez přístupu k Docker socketu nebo NAS credentials, pokud to není nezbytně a zvlášť zdokumentováno.
- V první fázi vůbec neotvírat síťový port. Povolení LAN přes firewall až po samostatném schválení.
- Nekopírovat zdrojové soubory SunFounder; používat samostatné kompatibilní knihovny dle licence.
