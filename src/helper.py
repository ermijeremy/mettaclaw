from collections import deque

def around_time(needle, k):
    filename = "repos/mettaclaw/memory/history.metta"
    before = deque(maxlen=k)
    after_remaining = 0
    ret = ""
    with open(filename, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if after_remaining > 0:
                ret += f"{lineno}:{line}"
                after_remaining -= 1
                continue
            if needle in line:
                start = lineno - len(before)
                for i, prev in enumerate(before, start):
                    ret += f"{i}:{prev}"
                ret += f"{lineno}:{line}"
                after_remaining = k
                before.clear()
            else:
                before.append(line)
    return ret

def balance_parentheses(s):
    s=s.replace("_quote_", '"')
    s = s.strip()
    left = 0
    while left < len(s) and s[left] == '(':
        left += 1
    right = 0
    while right < len(s) and s[len(s) - 1 - right] == ')':
        right += 1
    core = s[left:len(s) - right if right else len(s)].strip()
    return f"(({core}))"
