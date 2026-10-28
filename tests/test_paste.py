from pathlib import Path

import pytest

from polledge.config import load_election
from polledge.odds.paste import parse_pasted_odds

ROOT = Path(__file__).resolve().parents[1]

# Typical copy of a market page: name and odds on separate lines, with UI noise around.
STACKED = """
Politika
Francie - prezidentské volby 2027
Vítěz
Marine Le Pen
1.45
Édouard Philippe
4,20
Jean-Luc Mélenchon
15.00
Vsadit
+ 12 dalších
"""

# Other layout: name and odds on the same line, surname first.
INLINE = """
Dlouhodobé sázky
Le Pen Marine 1.50
Philippe Édouard 3.90
Glucksmann Raphaël 19
Celkem 3 výsledky
"""


def test_parse_stacked_layout():
    pairs = [(s.name, s.odds) for s in parse_pasted_odds(STACKED)]
    assert pairs == [("Marine Le Pen", 1.45), ("Édouard Philippe", 4.2), ("Jean-Luc Mélenchon", 15.0)]


def test_parse_inline_layout():
    pairs = [(s.name, s.odds) for s in parse_pasted_odds(INLINE)]
    assert pairs == [("Le Pen Marine", 1.5), ("Philippe Édouard", 3.9), ("Glucksmann Raphaël", 19.0)]


def test_parse_ignores_noise_and_duplicates():
    text = "Vsadit\n1.85\nMarine Le Pen\n1.45\nMarine Le Pen\n1.40\n2027\nLe Pen\n0.5\n"
    pairs = [(s.name, s.odds) for s in parse_pasted_odds(text)]
    assert pairs == [("Marine Le Pen", 1.45)]  # 'Vsadit' is UI text, the first occurrence wins, 0.5 is no odds


@pytest.mark.parametrize("text, expected", [
    ("Marine Le Pen", "LE_PEN"), ("Le Pen Marine", "LE_PEN"), ("Le Pen", "LE_PEN"),
    ("Philippe Édouard", "PHILIPPE"), ("Nikdo Neznámý", None),
])
def test_match_candidate(text, expected):
    from polledge.web.views import match_candidate

    fr = load_election(ROOT / "config" / "elections" / "fr_pres_2027.yaml")
    assert match_candidate(fr, text) == expected


# A screenshot converted to text by an AI chat: Markdown heading and table, long shots at 5000.
MARKDOWN = """
# České prezidentské volby 2028

Vítěz

| Kandidát                 | Kurz    |
| ------------------------ | ------- |
| Petr Pavel               | 1.33    |
| Andrej Babiš             | 15.00   |
| **Pavel Fischer**        | 30,00   |
| Agáta Hanychová          | 5000.00 |

Konec přijímání sázek: 31. 12. v 17:00
"""


def test_parse_markdown_table():
    pairs = [(s.name, s.odds) for s in parse_pasted_odds(MARKDOWN)]
    assert pairs == [("Petr Pavel", 1.33), ("Andrej Babiš", 15.0), ("Pavel Fischer", 30.0),
                     ("Agáta Hanychová", 5000.0)]


def test_parse_tab_separated_rows():
    pairs = [(s.name, s.odds) for s in parse_pasted_odds("Kandidát\tKurz\nPetr Pavel\t1,33\nAndrej Babiš\t15\n")]
    assert pairs == [("Petr Pavel", 1.33), ("Andrej Babiš", 15.0)]
