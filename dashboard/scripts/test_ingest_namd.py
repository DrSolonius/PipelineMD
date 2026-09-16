import struct
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ingest_namd import update_structures


class StructuralMetricsTests(unittest.TestCase):
    def test_rmsd_uses_only_rotating_coor_old_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdb = "".join(
                f"ATOM  {index:5d}  CA  ALA A{index:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00      PROA\n"
                for index, (x, y, z) in enumerate(
                    [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], 1
                )
            )
            (root / "step5_input.pdb").write_text(pdb, encoding="utf-8")
            coor = root / "step6.1_equilibration.coor.old"
            xsc = root / "step6.1_equilibration.xsc.old"
            (root / "step6.1_equilibration.dcd").write_bytes(b"invalid dcd must be ignored")

            for step, coordinates in [
                (100, [(0.0, 0.0, 0.0), (1.1, 0.0, 0.0), (0.0, 1.0, 0.0)]),
                (200, [(0.0, 0.0, 0.0), (1.2, 0.0, 0.0), (0.0, 1.0, 0.0)]),
            ]:
                values = np.asarray(coordinates, dtype="<f8")
                coor.write_bytes(struct.pack("<i", len(values)) + values.tobytes())
                xsc.write_text(f"# NAMD extended system\n{step} 1 0 0 0 1 0 0 0 1\n", encoding="utf-8")
                rmsd, _ = update_structures(root, root / "structural_state.npz")

            self.assertEqual([point["step"] for point in rmsd], [100, 200])
            self.assertTrue(all(point["source"].endswith(".coor.old") for point in rmsd))


if __name__ == "__main__":
    unittest.main()
