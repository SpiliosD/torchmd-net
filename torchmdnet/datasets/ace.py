import hashlib
import h5py
import numpy as np
import os
import torch as pt
from torch_geometric.data import Dataset, Data
from tqdm import tqdm


class Ace(Dataset):
    def __init__(
        self,
        root=None,
        transform=None,
        pre_transform=None,
        pre_filter=None,
        paths=None,
        max_gradient=None,
        subsample_molecules=1,
    ):

        arg_hash = f"{paths}{max_gradient}{subsample_molecules}"
        arg_hash = hashlib.md5(arg_hash.encode()).hexdigest()
        self.name = f"{self.__class__.__name__}-{arg_hash}"
        self.paths = paths
        self.max_gradient = max_gradient
        self.subsample_molecules = int(subsample_molecules)
        super().__init__(root, transform, pre_transform, pre_filter)

        (
            idx_name,
            z_name,
            pos_name,
            y_name,
            neg_dy_name,
            q_name,
            pq_name,
            dp_name,
        ) = self.processed_paths
        self.idx_mm = np.memmap(idx_name, mode="r", dtype=np.int64)
        self.z_mm = np.memmap(z_name, mode="r", dtype=np.int8)
        self.pos_mm = np.memmap(
            pos_name, mode="r", dtype=np.float32, shape=(self.z_mm.shape[0], 3)
        )
        self.y_mm = np.memmap(y_name, mode="r", dtype=np.float64)
        self.neg_dy_mm = np.memmap(
            neg_dy_name, mode="r", dtype=np.float32, shape=(self.z_mm.shape[0], 3)
        )
        self.q_mm = np.memmap(q_name, mode="r", dtype=np.int8)
        self.pq_mm = np.memmap(pq_name, mode="r", dtype=np.float32)
        self.dp_mm = np.memmap(
            dp_name, mode="r", dtype=np.float32, shape=(self.y_mm.shape[0], 3)
        )

        assert self.idx_mm[0] == 0
        assert self.idx_mm[-1] == len(self.z_mm)
        assert len(self.idx_mm) == len(self.y_mm) + 1

    @property
    def raw_paths(self):

        if os.path.isfile(self.paths):
            return [self.paths]
        if os.path.isdir(self.paths):
            return [
                os.path.join(self.paths, file_)
                for file_ in os.listdir(self.paths)
                if file_.endswith(".h5")
            ]

        raise RuntimeError(f"Cannot load {self.paths}")

    def sample_iter(self, mol_ids=False):

        assert self.subsample_molecules > 0

        # for path in tqdm(self.raw_paths, desc="Files"):
        for path in self.raw_paths:
            mols = h5py.File(path).items()

            for i_mol, (mol_id, mol) in tqdm(
                enumerate(mols),
                desc="Molecules",
                total=len(mols),
                leave=False,
            ):

                # Subsample molecules
                if i_mol % self.subsample_molecules != 0:
                    continue

                z = pt.tensor(mol["atomic_numbers"], dtype=pt.long)
                fq = pt.tensor(mol["formal_charges"], dtype=pt.long)
                q = fq.sum()

                for conf in mol["conformations"].values():

                    # Skip failed calculations
                    if "formation_energy" not in conf:
                        continue

                    assert conf["positions"].attrs["units"] == "Å"
                    pos = pt.tensor(conf["positions"][...], dtype=pt.float32)
                    assert pos.shape == (z.shape[0], 3)

                    assert conf["formation_energy"].attrs["units"] == "eV"
                    y = pt.tensor(conf["formation_energy"][()], dtype=pt.float64)
                    assert y.shape == ()

                    assert conf["forces"].attrs["units"] == "eV/Å"
                    neg_dy = -pt.tensor(conf["forces"][...], dtype=pt.float32)
                    assert neg_dy.shape == pos.shape

                    assert conf["partial_charges"].attrs["units"] == "e"
                    pq = pt.tensor(conf["partial_charges"][:], dtype=pt.float32)
                    assert pq.shape == z.shape

                    assert conf["dipole_moment"].attrs["units"] == "e*Å"
                    dp = pt.tensor(conf["dipole_moment"][:], dtype=pt.float32)
                    assert dp.shape == (3,)

                    # Skip samples with large forces
                    if self.max_gradient:
                        if neg_dy.norm(dim=1).max() > float(self.max_gradient):
                            continue

                    # Create a sample
                    args = dict(
                        z=z, pos=pos, y=y.view(1, 1), neg_dy=neg_dy, q=q, pq=pq, dp=dp
                    )
                    if mol_ids:
                        args["mol_id"] = mol_id
                    data = Data(**args)

                    if self.pre_filter is not None and not self.pre_filter(data):
                        continue

                    if self.pre_transform is not None:
                        data = self.pre_transform(data)

                    yield data

    @property
    def processed_file_names(self):
        return [
            f"{self.name}.idx.mmap",
            f"{self.name}.z.mmap",
            f"{self.name}.pos.mmap",
            f"{self.name}.y.mmap",
            f"{self.name}.neg_dy.mmap",
            f"{self.name}.q.mmap",
            f"{self.name}.pq.mmap",
            f"{self.name}.dp.mmap",
        ]

    def process(self):

        print("Arguments")
        print(f"  max_gradient: {self.max_gradient} eV/A")
        print(f"  subsample_molecules: {self.subsample_molecules}\n")

        print("Gathering statistics...")
        num_all_confs = 0
        num_all_atoms = 0
        for data in self.sample_iter():
            num_all_confs += 1
            num_all_atoms += data.z.shape[0]

        print(f"  Total number of conformers: {num_all_confs}")
        print(f"  Total number of atoms: {num_all_atoms}")

        (
            idx_name,
            z_name,
            pos_name,
            y_name,
            neg_dy_name,
            q_name,
            pq_name,
            dp_name,
        ) = self.processed_paths
        idx_mm = np.memmap(
            idx_name + ".tmp", mode="w+", dtype=np.int64, shape=num_all_confs + 1
        )
        z_mm = np.memmap(z_name + ".tmp", mode="w+", dtype=np.int8, shape=num_all_atoms)
        pos_mm = np.memmap(
            pos_name + ".tmp", mode="w+", dtype=np.float32, shape=(num_all_atoms, 3)
        )
        y_mm = np.memmap(
            y_name + ".tmp", mode="w+", dtype=np.float64, shape=num_all_confs
        )
        neg_dy_mm = np.memmap(
            neg_dy_name + ".tmp", mode="w+", dtype=np.float32, shape=(num_all_atoms, 3)
        )
        q_mm = np.memmap(q_name + ".tmp", mode="w+", dtype=np.int8, shape=num_all_confs)
        pq_mm = np.memmap(
            pq_name + ".tmp", mode="w+", dtype=np.float32, shape=num_all_atoms
        )
        dp_mm = np.memmap(
            dp_name + ".tmp", mode="w+", dtype=np.float32, shape=(num_all_confs, 3)
        )

        print("Storing data...")
        i_atom = 0
        for i_conf, data in enumerate(self.sample_iter()):
            i_next_atom = i_atom + data.z.shape[0]

            idx_mm[i_conf] = i_atom
            z_mm[i_atom:i_next_atom] = data.z.to(pt.int8)
            pos_mm[i_atom:i_next_atom] = data.pos
            y_mm[i_conf] = data.y
            neg_dy_mm[i_atom:i_next_atom] = data.neg_dy
            q_mm[i_conf] = data.q.to(pt.int8)
            pq_mm[i_atom:i_next_atom] = data.pq
            dp_mm[i_conf] = data.dp

            i_atom = i_next_atom

        idx_mm[-1] = num_all_atoms
        assert i_atom == num_all_atoms

        idx_mm.flush()
        z_mm.flush()
        pos_mm.flush()
        y_mm.flush()
        neg_dy_mm.flush()
        q_mm.flush()
        pq_mm.flush()
        dp_mm.flush()

        os.rename(idx_mm.filename, idx_name)
        os.rename(z_mm.filename, z_name)
        os.rename(pos_mm.filename, pos_name)
        os.rename(y_mm.filename, y_name)
        os.rename(neg_dy_mm.filename, neg_dy_name)
        os.rename(q_mm.filename, q_name)
        os.rename(pq_mm.filename, pq_name)
        os.rename(dp_mm.filename, dp_name)

    def len(self):
        return len(self.y_mm)

    def get(self, idx):

        atoms = slice(self.idx_mm[idx], self.idx_mm[idx + 1])
        z = pt.tensor(self.z_mm[atoms], dtype=pt.long)
        pos = pt.tensor(self.pos_mm[atoms], dtype=pt.float32)
        y = pt.tensor(self.y_mm[idx], dtype=pt.float32).view(
            1, 1
        )  # It would be better to use float64, but the trainer complaints
        neg_dy = pt.tensor(self.neg_dy_mm[atoms], dtype=pt.float32)
        q = pt.tensor(self.q_mm[idx], dtype=pt.long)
        pq = pt.tensor(self.pq_mm[atoms], dtype=pt.float32)
        dp = pt.tensor(self.dp_mm[idx], dtype=pt.float32)

        return Data(z=z, pos=pos, y=y, neg_dy=neg_dy, q=q, pq=pq, dp=dp)
