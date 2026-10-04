"""Offline UI examples; no network or HYSYS calls."""
FAKE_KEY = 'sk-' + 'w' * 30

TOLUENE_FACTS = {
    'species': ['甲苯', '苯', '邻二甲苯'],
    'feed_composition': [{'name': '甲苯', 'fraction': 1.0}],
    'composition_basis': 'pure',
    'reactions': [{'name': '歧化',
                   'species': [{'name': '甲苯', 'coefficient': -2},
                               {'name': '苯', 'coefficient': 1},
                               {'name': '邻二甲苯', 'coefficient': 1}],
                   'reversible': False}],
    'conversion_percent': 50, 'conversion_basis': '甲苯',
    'feed_total': 10000, 'feed_unit': 'kg/h',
    'feed_temperature': 380, 'feed_temperature_unit': '℃',
    'feed_pressure': 2.5, 'feed_pressure_unit': 'MPa',
    'case_pressures': [], 'case_pressure_unit': '',
    'outlet_temperatures': [], 'outlet_temperature_unit': '',
    'missing_information': [],
}

GASIFICATION_FACTS = {
    'species': ['碳', '水', '一氧化碳', '氢气'],
    'feed_composition': [{'name': '煤炭', 'fraction': 62},
                         {'name': '水', 'fraction': 38}],
    'composition_basis': 'mass_percent',
    'reactions': [{'name': '气化',
                   'species': [{'name': '碳', 'coefficient': -1},
                               {'name': '水', 'coefficient': -1},
                               {'name': '一氧化碳', 'coefficient': 1},
                               {'name': '氢气', 'coefficient': 1}],
                   'reversible': False}],
    'conversion_percent': None, 'conversion_basis': '',
    'feed_total': 80000, 'feed_unit': 'Nm3/h',
    'feed_temperature': 40, 'feed_temperature_unit': 'C',
    'feed_pressure': 40, 'feed_pressure_unit': 'bar',
    'case_pressures': [], 'case_pressure_unit': '',
    'outlet_temperatures': [1400], 'outlet_temperature_unit': 'C',
    'missing_information': [],
}

GASIFICATION_TEXT = ('水煤浆气化：C+H2O → CO+H2，进料煤炭和水，流量80000Nm3/h，'
                     '压力40bar，进料温度40摄氏度，出口1400度，浓度62wt%')

TOLUENE_TEXT = ('甲苯歧化 2C₇H₈ → C₆H₆ + C₈H₁₀，甲苯进料流量10000kg/h，'
                '进料温度380℃，压力2.5MPa，转化率50%')


