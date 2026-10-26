import ast

NL = chr(10)
esc_n = chr(92) + 'n'
q = chr(34)
p = 'tests/test_gateway_golden.py'
raw = open(p, encoding='utf-8', newline='').read()
broken = ('    assert ' + q + '## Entity notes' + NL + NL + '### Blade (Item)' + NL + NL + 'Sharp.' + NL + NL + '  Kept oiled.' + NL + q + ' in t, t' + NL
          + '    assert t.endswith(' + q + 'Kept oiled.' + NL + q + '), t[-60:]  # the final rstrip takes the last note' + chr(39) + 's trailing spaces' + NL)
good = ('    assert ' + q + '## Entity notes' + esc_n + esc_n + '### Blade (Item)' + esc_n + esc_n + 'Sharp.' + esc_n + esc_n + '  Kept oiled.' + esc_n + q + ' in t, t' + NL
        + '    assert t.endswith(' + q + 'Kept oiled.' + esc_n + q + '), t[-60:]  # the final rstrip takes the last note' + chr(39) + 's trailing spaces' + NL)
assert broken in raw, 'broken block not found'
raw = raw.replace(broken, good, 1)
open(p, 'w', encoding='utf-8', newline='').write(raw)
ast.parse(raw)
print('repaired to escaped form')
