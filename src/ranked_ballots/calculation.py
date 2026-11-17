def count(candidates,ballots):
    active=set(candidates);rounds=[];winner=None
    while active:
        totals={c:0 for c in sorted(active)};exhausted=0
        for ranking,weight in ballots:
            choice=next((c for c in ranking if c in active),None)
            if choice is None:exhausted+=weight
            else:totals[choice]+=weight
        continuing=sum(totals.values());row={'round':len(rounds)+1,'tallies':totals,'continuing_weight':continuing,'exhausted_weight':exhausted,'eliminated':None}
        majority=[c for c in active if totals[c]*2>continuing]
        if continuing==0:row['outcome']='no_continuing_ballots';rounds.append(row);break
        if majority:winner=majority[0];row['outcome']='majority';rounds.append(row);break
        eliminated=min(active,key=lambda c:(totals[c],c));row['eliminated']=eliminated;row['outcome']='elimination';rounds.append(row);active.remove(eliminated)
    return {'winner':winner,'rounds':rounds,'total_weight':sum(w for ranking,w in ballots),'tie_policy':'single lowest tally, lexicographically smallest ID eliminated'}
