import unittest
import numpy as np
from research.direction_model import examples, split_indices, VAL_START, TEST_START, FEATURES


class DirectionModelTests(unittest.TestCase):
    def rows(self):
        start=VAL_START-120*86400000
        return [[start+i*86400000,100+i,102+i,99+i,101+i,1000+i,2000+i] for i in range(200)]

    def test_future_changes_do_not_change_past_features(self):
        rows=self.rows(); changed=[r[:] for r in rows]
        for row in changed[140:]:
            for index in (1,2,3,4):row[index]*=2
        meta=dict(expire_datetime=None,volume_multiple=10,price_tick=1)
        a=examples(rows,86400,10,meta);b=examples(changed,86400,10,meta)
        cutoff=rows[140][0]
        mask=a[:,-1]<cutoff
        np.testing.assert_allclose(a[mask],b[mask])

    def test_purge_removes_boundary_crossing_labels(self):
        values=np.zeros((4,len(FEATURES)+4))
        values[:,-2]=[VAL_START-1,VAL_START-10,VAL_START,TEST_START]
        values[:,-1]=[VAL_START+1,VAL_START-1,TEST_START+1,TEST_START+2]
        masks=split_indices(values)
        self.assertEqual(masks['train'].tolist(),[False,True,False,False])
        self.assertEqual(masks['validation'].tolist(),[False,False,False,False])
        self.assertEqual(masks['test'].tolist(),[False,False,False,True])

    def test_missing_target_day_does_not_use_later_quote(self):
        rows=self.rows()
        calendar=[r[0] for r in rows]
        missing=rows[140][0]
        before=rows[139][0]+15*3600000
        rows=[r for r in rows if r[0]!=missing]
        values=examples(rows,86400,1,dict(expire_datetime=None,volume_multiple=10,price_tick=1),trading_days=calendar)
        self.assertFalse((values[:,-2]==before).any())


if __name__=='__main__':unittest.main()
