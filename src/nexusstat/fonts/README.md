# Bitmapová písma OLED

Písma pro vykreslení displeje 128 × 64 (`src/nexusstat/render.py`). Jsou přiložená
v projektu, aby vzhled nezávisel na písmech nainstalovaných v systému.

| Soubor | Písmo (XLFD) | Použití |
| --- | --- | --- |
| `8x13B.pcf.gz` | `-Misc-Fixed-Bold-R-Normal--13-120-75-75-C-80-ISO10646-1` | nápis NEXUS, nadpisy stránek, hodiny (2×), hodnoty (`48°C`) a stavy (CRIT, WARN, OK …) |
| `6x13.pcf.gz` | `-Misc-Fixed-Medium-R-SemiCondensed--13-120-75-75-C-60-ISO10646-1` | názvy kontrol, popisy ve STATUS, datum, značka SIM |

Užší tučná 6x13B a 7x13B nejsou použitá: na skutečné velikosti v nich splývá „M“ a „W“ s „H“
(„SIM“ se čte jako „SIH“, „WARN“ jako „HARN“).

## Původ

Klasická bitmapová písma X11 „Fixed“ (misc-fixed, Unicode verze z projektu
ucs-fonts). Soubory jsou beze změny zkopírované z balíčku Debian/Ubuntu
`xfonts-base` 1:1.0.5+nmu1 (`/usr/share/fonts/X11/misc/`).

SHA-256:

```text
08a0eb134120be2afb31580a7daf70e0c8001f1b21fce5ed5ff5db9bd6a190bf  6x13.pcf.gz
aaab8f967fed47f9baf3f43f919975e2d5510c710a9a00ff3cd6a6d5491747cf  8x13B.pcf.gz
```

## Licence

Volné dílo (public domain). Vlastnost `COPYRIGHT` přímo v každém písmu zní:

> Public domain font.  Share and enjoy.

Totéž uvádí soubor copyright balíčku `xfonts-base` (`font-misc-misc/COPYING`).
Šíření ani úpravy nemají žádné podmínky; tento soubor slouží jen k doložení původu.
