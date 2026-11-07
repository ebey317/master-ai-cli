"""Expression evaluator."""


def evaluate(expr: str, env: dict) -> float:
    """Evaluate a tiny expression language."""
    return _eval(_tokenize(expr), env)


def _tokenize(expr):
    tokens = []
    i = 0
    while i < len(expr):
        c = expr[i]
        if c.isspace():
            i += 1
        elif c in "+-*/()":
            tokens.append(c)
            i += 1
        elif c.isdigit() or c == ".":
            j = i
            while j < len(expr) and (expr[j].isdigit() or expr[j] == "."):
                j += 1
            tokens.append(float(expr[i:j]) if "." in expr[i:j] else int(expr[i:j]))
            i = j
        elif c.isalpha():
            j = i
            while j < len(expr) and expr[j].isalpha():
                j += 1
            tokens.append(expr[i:j])
            i = j
        else:
            raise ValueError(f"invalid char {c!r}")
    return tokens


def _eval(tokens, env):
    pos = [0]

    def parse_expr():
        value = parse_term()
        while pos[0] < len(tokens) and tokens[pos[0]] in ("+", "-"):
            op = tokens[pos[0]]
            pos[0] += 1
            rhs = parse_term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def parse_term():
        value = parse_factor()
        while pos[0] < len(tokens) and tokens[pos[0]] in ("*", "/"):
            op = tokens[pos[0]]
            pos[0] += 1
            rhs = parse_factor()
            value = value * rhs if op == "*" else value / rhs
        return value

    def parse_factor():
        tok = tokens[pos[0]]
        pos[0] += 1
        if tok == "(":
            value = parse_expr()
            if tokens[pos[0]] != ")":
                raise ValueError("missing )")
            pos[0] += 1
            return value
        if isinstance(tok, str):
            if tok not in env:
                raise ValueError(f"undefined {tok}")
            return env[tok]
        return tok

    return parse_expr()
