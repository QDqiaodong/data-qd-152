
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,math
from pathlib import Path
from fractions import Fraction
from ranked_ballots.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_transfer(self):
        r=analyze(sample());self.assertEqual(r['winner'],'B');self.assertEqual(r['rounds'][0]['eliminated'],'C');self.assertEqual(r['rounds'][1]['tallies'],{'A':4,'B':5})
    def test_tie_policy(self):
        d={'candidates':['A','B'],'ballots':[{'id':'a','ranking':['A'],'weight':1},{'id':'b','ranking':['B'],'weight':1}]};r=analyze(d);self.assertEqual(r['rounds'][0]['eliminated'],'A');self.assertEqual(r['winner'],'B');self.assertEqual(r['rounds'][1]['exhausted_weight'],1)
    def test_no_continuing(self):
        d=sample();d['ballots']=[{'id':'empty','ranking':[],'weight':5}];r=analyze(d);self.assertIsNone(r['winner']);self.assertEqual(r['rounds'][0]['exhausted_weight'],5)
    def test_duplicate_rank(self):
        d=sample();d['ballots'][0]['ranking']=['A','A'];self.bad(d)
    def test_unknown_candidate(self):
        d=sample();d['ballots'][0]['ranking']=['X'];self.bad(d)
    def test_order(self):
        d=sample();d['ballots'].reverse();d['candidates'].reverse();self.assertEqual(analyze(d),analyze(sample()))
    def test_vote_conservation(self):
        rng=random.Random(13)
        for _ in range(30):
            cs=list('ABCD');d={'candidates':cs,'ballots':[{'id':str(i),'ranking':rng.sample(cs,rng.randrange(5)),'weight':rng.randrange(1,10)} for i in range(20)]};r=analyze(d)
            for row in r['rounds']:self.assertEqual(sum(row['tallies'].values())+row['exhausted_weight'],r['total_weight'])
            self.assertLessEqual(len(r['rounds']),len(cs))

class Command(unittest.TestCase):
    def call(self,src,out):
        return subprocess.run([sys.executable,'-m','ranked_ballots.cli','--source',str(src),'--output',str(out)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
