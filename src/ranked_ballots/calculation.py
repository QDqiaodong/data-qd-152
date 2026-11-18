def count(candidates,ballots):
    active=set(candidates);rounds=[];winner=None
    # Per-ballot cursor: the first position whose candidate can still be active.
    # An eliminated candidate never returns to ``active``, so advancing this
    # cursor is monotonic and yields exactly the same first continuing choice
    # as rescanning the whole ranking from position 0 every round.
    cursors=[0]*len(ballots)
    while active:
        totals={c:0 for c in sorted(active)};exhausted=0
        for index,(ranking,weight) in enumerate(ballots):
            position=cursors[index];length=len(ranking)
            while position<length and ranking[position] not in active:position+=1
            cursors[index]=position
            if position==length:exhausted+=weight
            else:totals[ranking[position]]+=weight
        continuing=sum(totals.values());row={'round':len(rounds)+1,'tallies':totals,'continuing_weight':continuing,'exhausted_weight':exhausted,'eliminated':None}
        majority=[c for c in active if totals[c]*2>continuing]
        if continuing==0:row['outcome']='no_continuing_ballots';rounds.append(row);break
        if majority:winner=majority[0];row['outcome']='majority';rounds.append(row);break
        eliminated=min(active,key=lambda c:(totals[c],c));row['eliminated']=eliminated;row['outcome']='elimination';rounds.append(row);active.remove(eliminated)
    return {'winner':winner,'rounds':rounds,'total_weight':sum(w for ranking,w in ballots),'tie_policy':'single lowest tally, lexicographically smallest ID eliminated'}
