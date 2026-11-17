from .common import require,ident,number,rows
from .calculation import count

def analyze(data):
    require(data,'candidates ballots');candidates=[ident(x) for x in rows(data['candidates'],100)]
    if not candidates or len(set(candidates))!=len(candidates):raise ValueError('unique candidates required')
    ballots=[];seen=set()
    for b in rows(data['ballots'],20000):
        require(b,'id ranking weight');bid=ident(b['id']);ranking=[ident(x) for x in rows(b['ranking'],100)];weight=number(b['weight'],1,10**9)
        if bid in seen or len(set(ranking))!=len(ranking) or any(x not in candidates for x in ranking):raise ValueError('duplicate ballot, ranking, or unknown candidate')
        seen.add(bid);ballots.append((ranking,weight))
    return count(candidates,ballots)
