# Claude Code: NexusSTAT

1. **Nejdřív čti:** `README.md`, `manifest.toml`, `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`. Při práci na Nexusu navíc platný `/srv/nexus/docs/PROJECT-STANDARD.md` a `/srv/nexus/PROJECTS.md`. Na notebooku je automaticky nepředpokládej.
2. **Skutečnost vs. návrh:** NexusSTAT je ve stavu `development` (jen lokální vývoj na notebooku), není na Raspberry Pi nasazen. AnimeDB také není na Nexusu. Existující `pironman5.service` (Pironman 1.3.20, pm_auto 2.0.6) zatím musí zůstat beze změny.
3. **První úkol po schválení:** jednoduchý čistě offline Python simulátor Core a OLED (128×64) s testy; žádný GPIO, I²C, SPI, evdev, NAS, Docker socket, InfluxDB nebo systémové služby. Před implementací shrň rozsah souborů.
4. **Styl:** jednoduché čitelné funkce; smysluplná jména; české komentáře vysvětlující proč, ne každý zřejmý řádek; minimum závislostí; nevytvářej framework ani rozsáhlé abstrakce.
5. **Knihovny:** pro pozdější hardware preferuj Adafruit SSD1306 a NeoPixel SPI; nekopíruj žádný kód SunFounder. Chlazení drž oddělené od diagnostiky.
6. **Bezpečnost:** žádné zásahy do konfigurace Nexusu, ventilátorů, tlačítka, OLED/RGB, firewallu, Resticu, produkční DB a NAS bez výslovného schválení. Nic neinstaluj/nerestartuj/necommituj/nepushuj bez schválení. Žádná tajemství či interní IP ve veřejném repozitáři.
7. **Pracovní postup:** malá změna → test → kontrola → stručné výsledky. Nezaměňuj simulovaný test s hardware testem. Před rizikovou změnou zdokumentuj rollback.
8. **Dokumentace:** při návrhových změnách aktualizuj relevantní `docs/` a při změně stavu i `manifest.toml`. Po každém úkolu uveď změněné soubory, ověření, rizika, Git stav a co čeká na schválení.
