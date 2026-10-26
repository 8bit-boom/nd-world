import ast

NL = chr(10)
p = 'tests/test_gateway_golden.py'
lines = open(p, encoding='utf-8', newline='').read().splitlines(keepends=True)
assert 'Kept oiled' in lines[4648] and 'Kept oiled' in lines[4649], (lines[4648], lines[4649])
q = chr(34)  # double quote
esc_n = chr(92) + 'n'  # the two characters backslash + n
lines[4648] = '    assert ' + q + '## Entity notes' + esc_n + esc_n + '### Blade (Item)' + esc_n + esc_n + 'Sharp.' + esc_n + esc_n + '  Kept oiled.' + esc_n + q + ' in t, t' + NL
lines[4649] = '    assert t.endswith(' + q + 'Kept oiled.' + esc_n + q + '), t[-60:]  # the final rstrip takes the last note' + chr(39) + 's trailing spaces' + NL
s = ''.join(lines)
open(p, 'w', encoding='utf-8', newline='').write(s)
ast.parse(s)
print('patched cleanly')
