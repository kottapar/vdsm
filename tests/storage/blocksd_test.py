#
# Copyright 2014-2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division

from collections import namedtuple
from contextlib import contextmanager
import os
import time
import uuid
import string

import pytest

from vdsm.common.units import MiB, GiB
from vdsm.storage import blockSD
from vdsm.storage import clusterlock
from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import image
from vdsm.storage import lvm
from vdsm.storage import qemuimg
from vdsm.storage import sanlock_direct
from vdsm.storage import sd
from vdsm.storage import sp
from vdsm.storage.sdc import sdCache
from vdsm.storage.spbackends import StoragePoolDiskBackend

from . import qemuio
from . marks import requires_root

from storage.storagefakelib import fake_spm
from storage.storagefakelib import fake_vg
from storage.storagefakelib import FakeDomainMonitor
from storage.storagefakelib import FakeTaskManager

TESTDIR = os.path.dirname(__file__)


Chain = namedtuple("Chain", ["base", "internal", "top"])


class TestMetadataValidity:

    MIN_MD_SIZE = blockSD.VG_METADATASIZE * MiB // 2
    MIN_MD_FREE = MIN_MD_SIZE * blockSD.VG_MDA_MIN_THRESHOLD

    def test_valid_ok(self):
        vg = fake_vg(
            vg_mda_size=self.MIN_MD_SIZE, vg_mda_free=self.MIN_MD_FREE)
        assert blockSD.metadataValidity(vg)['mdavalid']

    def test_valid_bad(self):
        vg = fake_vg(
            vg_mda_size=self.MIN_MD_SIZE - 1, vg_mda_free=self.MIN_MD_FREE)
        assert not blockSD.metadataValidity(vg)['mdavalid']

    def test_threshold_ok(self):
        vg = fake_vg(
            vg_mda_size=self.MIN_MD_SIZE, vg_mda_free=self.MIN_MD_FREE + 1)
        assert blockSD.metadataValidity(vg)['mdathreshold']

    def test_threshold_bad(self):
        vg = fake_vg(
            vg_mda_size=self.MIN_MD_SIZE, vg_mda_free=self.MIN_MD_FREE)
        assert not blockSD.metadataValidity(vg)['mdathreshold']


class DomainFactory:

    def __init__(self, tmp_storage, tmp_repo):
        self.tmp_storage = tmp_storage
        self.tmp_repo = tmp_repo

    def create_domain(self, sd_uuid, name="domain", version=5):
        dev = self.tmp_storage.create_device(20 * GiB)
        lvm.createVG(sd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
        vg = lvm.getVG(sd_uuid)

        dom = blockSD.BlockStorageDomain.create(
            sdUUID=sd_uuid,
            domainName=name,
            domClass=sd.DATA_DOMAIN,
            vgUUID=vg.uuid,
            version=version,
            storageType=sd.ISCSI_DOMAIN)

        # Create domain directory structure.
        dom.refresh()

        # Attach repo pool - SD expects at least one pool is attached.
        dom.attach(self.tmp_repo.pool_id)
        sdCache.knownSDs[sd_uuid] = blockSD.findDomain
        sdCache.manuallyAddDomain(dom)

        return dom


@pytest.fixture
def domain_factory(tmp_storage, tmp_repo):
    return DomainFactory(tmp_storage, tmp_repo)


def fakeGetLV(vgName):
    """ This function returns lvs output in lvm.getLV() format.

    Input file name: lvs_<sdName>.out
    Input file should be the output of:
    lvs --noheadings --units b --nosuffix --separator '|' \
        -o uuid,name,vg_name,attr,size,seg_start_pe,devices,tags <sdName>

    """
    # TODO: simplify by returning fake lvs instead of parsing real lvs output.
    lvs_output = os.path.join(TESTDIR, 'lvs_%s.out' % vgName)
    lvs = []
    with open(lvs_output) as f:
        for line in f:
            fields = [field.strip() for field in line.split(lvm.SEPARATOR)]
            lvs.append(lvm.LV.fromlvm(*fields))
    return lvs


def make_lv(name=None, tags=()):
    return lvm.LV(
        tags=tags,
        uuid=None,
        name=name,
        vg_name=None,
        attr=None,
        size=None,
        seg_start_pe=None,
        devices=None,
        writeable=None,
        opened=None,
        active=None)


def create_chain(domain):
    img_uuid = str(uuid.uuid4())
    base_vol_id = str(uuid.uuid4())
    internal_vol_id = str(uuid.uuid4())
    top_vol_id = str(uuid.uuid4())

    vol_capacity = 10 * GiB

    domain.createVolume(
        imgUUID=img_uuid,
        capacity=vol_capacity,
        volFormat=sc.COW_FORMAT,
        preallocate=sc.SPARSE_VOL,
        diskType=sc.DATA_DISKTYPE,
        volUUID=base_vol_id,
        desc="Base volume",
        srcImgUUID=sc.BLANK_UUID,
        srcVolUUID=sc.BLANK_UUID)

    domain.createVolume(
        imgUUID=img_uuid,
        capacity=vol_capacity,
        volFormat=sc.COW_FORMAT,
        preallocate=sc.SPARSE_VOL,
        diskType=sc.DATA_DISKTYPE,
        volUUID=internal_vol_id,
        desc="Internal volume",
        srcImgUUID=sc.BLANK_UUID,
        srcVolUUID=base_vol_id)

    domain.createVolume(
        imgUUID=img_uuid,
        capacity=vol_capacity,
        volFormat=sc.COW_FORMAT,
        preallocate=sc.SPARSE_VOL,
        diskType=sc.DATA_DISKTYPE,
        volUUID=top_vol_id,
        desc="Top volume",
        srcImgUUID=sc.BLANK_UUID,
        srcVolUUID=internal_vol_id)

    # Get volumes.
    base_vol = domain.produceVolume(img_uuid, base_vol_id)
    internal_vol = domain.produceVolume(img_uuid, internal_vol_id)
    top_vol = domain.produceVolume(img_uuid, top_vol_id)

    # Load image.
    img = image.Image(top_vol.repoPath)

    # Check volume chain created correctly.
    chain = [v.volUUID for v in img.getChain(domain.sdUUID, img_uuid)]
    assert chain == [base_vol_id, internal_vol_id, top_vol_id]

    # Verify current chain parents.
    assert base_vol.getParent() == sc.BLANK_UUID
    assert internal_vol.getParent() == base_vol_id
    assert top_vol.getParent() == internal_vol_id

    return Chain(base_vol, internal_vol, top_vol)


@contextmanager
def change_vol_tag(vol, tag_prefix, tag_value):
    lv = lvm.getLV(vol.sdUUID, vol.volUUID)
    new_tags = {tag_prefix + tag_value}
    old_tags = {tag for tag in lv.tags
                if tag.startswith(tag_prefix)}

    lvm.changeLVsTags(
        vol.sdUUID, vol.volUUID, delTags=old_tags, addTags=new_tags)
    try:
        yield
    finally:
        lvm.changeLVsTags(
            vol.sdUUID, vol.volUUID, delTags=new_tags, addTags=old_tags)


@contextmanager
def delete_vol_tag(vol, tag_prefix):
    lv = lvm.getLV(vol.sdUUID, vol.volUUID)
    old_tags = {tag for tag in lv.tags
                if tag.startswith(tag_prefix)}

    lvm.changeLVsTags(
        vol.sdUUID, vol.volUUID, delTags=old_tags)
    try:
        yield
    finally:
        lvm.changeLVsTags(
            vol.sdUUID, vol.volUUID, addTags=old_tags)


class TestGetAllVolumes:
    # TODO: add more tests, see fileSDTests.py

    def test_volumes_count(self, monkeypatch):
        monkeypatch.setattr(lvm, 'getLV', fakeGetLV)
        sdName = "3386c6f2-926f-42c4-839c-38287fac8998"
        allVols = blockSD.getAllVolumes(sdName)
        assert len(allVols) == 23

    def test_missing_tags(self, monkeypatch):
        monkeypatch.setattr(lvm, 'getLV', fakeGetLV)
        sdName = "f9e55e18-67c4-4377-8e39-5833ca422bef"
        allVols = blockSD.getAllVolumes(sdName)
        assert len(allVols) == 2


class TestParseLVTags:

    def test_parse_tags(self):
        lv = make_lv(
            tags=("IU_6b055e16-337d-4cbe-9af9-d75f00f910c1",
                  "MD_93",
                  "PU_d6f2fcac-e98e-4330-9321-560146f40a84")
        )
        lvtags = blockSD.parse_lv_tags(lv)
        assert lvtags == blockSD.LVTags(
            mdslot=93,
            image="6b055e16-337d-4cbe-9af9-d75f00f910c1",
            parent="d6f2fcac-e98e-4330-9321-560146f40a84"
        )

    def test_parse_missing_tags(self):
        lv = make_lv(
            tags=("IU_6b055e16-337d-4cbe-9af9-d75f00f910c1",
                  "PU_d6f2fcac-e98e-4330-9321-560146f40a84")
        )
        lvtags = blockSD.parse_lv_tags(lv)
        assert lvtags == blockSD.LVTags(
            mdslot=None,
            image="6b055e16-337d-4cbe-9af9-d75f00f910c1",
            parent="d6f2fcac-e98e-4330-9321-560146f40a84"
        )

    def test_parse_invalid_tag(self):
        lv = make_lv(
            tags=("IU_6b055e16-337d-4cbe-9af9-d75f00f910c1",
                  "PU_d6f2fcac-e98e-4330-9321-560146f40a84",
                  "MD_INVALID_INT")
        )
        lvtags = blockSD.parse_lv_tags(lv)
        assert lvtags == blockSD.LVTags(
            mdslot=None,
            image="6b055e16-337d-4cbe-9af9-d75f00f910c1",
            parent="d6f2fcac-e98e-4330-9321-560146f40a84"
        )


class TestIterVolumes:

    def test_iter_volumes(self, monkeypatch):
        lvs = [
            make_lv(name="lv1"),
            make_lv(name="master"),
            make_lv(name="lv2", tags=(sc.TAG_VOL_UNINIT,)),
            make_lv(name="lv3")
        ]
        monkeypatch.setattr(lvm, 'getLV', lambda sd_uuid: lvs)

        # Expecting to have only user initialized volumes.
        expected_lvs = [
            make_lv(name="lv1"),
            make_lv(name="lv3")
        ]

        assert list(blockSD._iter_volumes("sd-id")) == expected_lvs


class TestOccupiedSlots:

    @pytest.mark.parametrize("lvs,expected", [
        pytest.param(
            [
                make_lv(tags=("MD_1",)),
                make_lv(tags=("MD_2",)),
                make_lv(tags=("MD_3",)),
            ],
            [1, 2, 3],
            id="parse-md-tags"),
        pytest.param(
            [
                make_lv(tags=("MD_1",)),
                make_lv(tags=("MD_bad-tag",)),
                make_lv(tags=("MD_3",)),
            ],
            [1, 3],
            id="bad-md-tag"),
        pytest.param(
            [
                make_lv(tags=("MD_1",)),
                make_lv(),
                make_lv(tags=("MD_3",)),
            ],
            [1, 3],
            id="missing-md-tag"),
    ])
    def test_occupied_slots(self, lvs, expected, monkeypatch):
        monkeypatch.setattr(lvm, 'getLV', lambda sd_uuid: lvs)
        occupied = blockSD._occupied_metadata_slots("sd-id")
        assert occupied == expected


class TestDecodeValidity:

    def test_all_keys(self):
        value = ('pv:myname,uuid:Gk8q,pestart:0,'
                 'pecount:77,mapoffset:0')
        pvinfo = blockSD.decodePVInfo(value)
        assert pvinfo["guid"] == 'myname'
        assert pvinfo["uuid"] == 'Gk8q'
        assert pvinfo["pestart"] == '0'
        assert pvinfo["pecount"] == '77'
        assert pvinfo["mapoffset"] == '0'

    def test_decode_pv_colon(self):
        pvinfo = blockSD.decodePVInfo('pv:my:name')
        assert pvinfo["guid"] == 'my:name'

    @pytest.mark.xfail(reason='Comma in PV name is not supported yet')
    def test_decode_pv_comma(self):
        pvinfo = blockSD.decodePVInfo('pv:my,name')
        assert pvinfo["guid"] == 'my,name'


def test_metadata_offset(monkeypatch):
    sd_uuid = str(uuid.uuid4())
    fake_metadata = {
        sd.DMDK_VERSION: 4,
        sd.DMDK_LOGBLKSIZE: 512,
        sd.DMDK_PHYBLKSIZE: 512,
    }

    monkeypatch.setattr(sd.StorageDomainManifest, "_makeDomainLock",
                        lambda _: None)
    sd_manifest = blockSD.BlockStorageDomainManifest(sd_uuid, fake_metadata)

    assert 0 == sd_manifest.metadata_offset(0)
    assert 51200 == sd_manifest.metadata_offset(100)
    assert 0 == sd_manifest.metadata_offset(0, version=4)
    assert 51200 == sd_manifest.metadata_offset(100, version=4)
    assert 1048576 == sd_manifest.metadata_offset(0, version=5)
    assert 1867776 == sd_manifest.metadata_offset(100, version=5)


@pytest.mark.parametrize("version,block_size", [
    # Before version 5 only 512 bytes is supported.
    (3, sc.BLOCK_SIZE_4K),
    (3, sc.BLOCK_SIZE_AUTO),
    (3, 42),
    (4, sc.BLOCK_SIZE_4K),
    (4, sc.BLOCK_SIZE_AUTO),
    (4, 42),
    # Version 5 will allow 4k soon.
    (5, sc.BLOCK_SIZE_4K),
    (5, sc.BLOCK_SIZE_AUTO),
    (5, 42),
])
def test_unsupported_block_size_rejected(version, block_size):
    # Note: assumes that validation is done before trying to reach storage.
    with pytest.raises(se.InvalidParameterException):
        blockSD.BlockStorageDomain.create(
            sdUUID=str(uuid.uuid4()),
            domainName="test",
            domClass=sd.DATA_DOMAIN,
            vgUUID=None,
            storageType=sd.ISCSI_DOMAIN,
            version=version,
            block_size=block_size)


def test_create_domain_unsupported_version():
    with pytest.raises(se.UnsupportedDomainVersion):
        blockSD.BlockStorageDomain.create(
            str(uuid.uuid4()),
            "test",
            sd.DATA_DOMAIN,
            None,  # vg-uuid
            sd.ISCSI_DOMAIN,
            0)


@requires_root
@pytest.mark.root
def test_attach_domain_unsupported_version(
        monkeypatch, tmp_storage, tmp_repo, fake_task, fake_sanlock):
    sd_uuid = str(uuid.uuid4())

    dev = tmp_storage.create_device(20 * GiB)
    lvm.createVG(sd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
    vg = lvm.getVG(sd_uuid)

    dom = blockSD.BlockStorageDomain.create(
        sdUUID=sd_uuid,
        domainName="domain",
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=3,
        storageType=sd.ISCSI_DOMAIN)

    sdCache.knownSDs[sd_uuid] = blockSD.findDomain

    # Set domain metadata to version 0
    metadata = """\
ALIGNMENT=1048576
BLOCK_SIZE=512
CLASS=Data
DESCRIPTION=storage domain
IOOPTIMEOUTSEC=10
LEASERETRIES=3
LEASETIMESEC=60
LOCKPOLICY=
LOCKRENEWALINTERVALSEC=5
POOL_UUID=
REMOTE_PATH=server:/path
ROLE=Regular
SDUUID={}
TYPE=LOCALFS
VERSION=0
""".format(sd_uuid)
    with dom.special_lvs():
        with open("/dev/{}/metadata".format(vg.name), "wb") as f:
            f.write(metadata.encode("utf-8"))

    # Remove domain metadata
    dom.setMetadata({})

    spm = fake_spm(
        tmp_repo.pool_id,
        0,
        {sd_uuid: sd.DOM_UNATTACHED_STATUS})

    # Since we removed support for V0 we can no longer read
    # the replaced metadata from storage and end up with missing
    # version key when trying to get version for attached domain
    with pytest.raises(se.InvalidMetadata):
        spm.attachSD(sd_uuid)


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [3, 4, 5])
def test_create_domain_metadata(tmp_storage, tmp_repo, fake_sanlock,
                                domain_version):
    sd_uuid = str(uuid.uuid4())
    domain_name = "loop-domain"

    dev1 = tmp_storage.create_device(10 * GiB)
    dev2 = tmp_storage.create_device(10 * GiB)
    lvm.createVG(sd_uuid, [dev1, dev2], blockSD.STORAGE_UNREADY_DOMAIN_TAG,
                 128)
    vg = lvm.getVG(sd_uuid)
    pv1 = lvm.getPV(dev1)
    pv2 = lvm.getPV(dev2)

    dom = blockSD.BlockStorageDomain.create(
        sdUUID=sd_uuid,
        domainName=domain_name,
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=domain_version,
        storageType=sd.ISCSI_DOMAIN)

    sdCache.knownSDs[sd_uuid] = blockSD.findDomain
    sdCache.manuallyAddDomain(dom)

    lease = sd.DEFAULT_LEASE_PARAMS
    expected = {
        # Common storage domain values.
        sd.DMDK_CLASS: sd.DATA_DOMAIN,
        sd.DMDK_DESCRIPTION: domain_name,
        sd.DMDK_IO_OP_TIMEOUT_SEC: lease[sd.DMDK_IO_OP_TIMEOUT_SEC],
        sd.DMDK_LEASE_RETRIES: lease[sd.DMDK_LEASE_RETRIES],
        sd.DMDK_LEASE_TIME_SEC: lease[sd.DMDK_LEASE_TIME_SEC],
        sd.DMDK_LOCK_POLICY: "",
        sd.DMDK_LOCK_RENEWAL_INTERVAL_SEC:
            lease[sd.DMDK_LOCK_RENEWAL_INTERVAL_SEC],
        sd.DMDK_POOLS: [],
        sd.DMDK_ROLE: sd.REGULAR_DOMAIN,
        sd.DMDK_SDUUID: sd_uuid,
        sd.DMDK_TYPE: sd.ISCSI_DOMAIN,
        sd.DMDK_VERSION: domain_version,

        # Block storge domain extra values.
        blockSD.DMDK_VGUUID: vg.uuid,

        # PV keys for blockSD.DMDK_PV_REGEX.
        "PV0": {
            'guid': os.path.basename(dev1),
            'mapoffset': '0',
            'pecount': '77',
            'pestart': '0',
            'uuid': pv1.uuid,
        },
        "PV1": {
            'guid': os.path.basename(dev2),
            'mapoffset': '77',
            'pecount': '77',
            'pestart': '0',
            'uuid': pv2.uuid,
        },
    }

    # In version 5 we removed LOGBLKSIZE and PHYBLKSIZE and added
    # ALIGNMENT and BLOCK_SIZE.
    if domain_version < 5:
        expected[sd.DMDK_LOGBLKSIZE] = sc.BLOCK_SIZE_512
        expected[sd.DMDK_PHYBLKSIZE] = sc.BLOCK_SIZE_512
    else:
        expected[sd.DMDK_ALIGNMENT] = sc.ALIGNMENT_1M
        expected[sd.DMDK_BLOCK_SIZE] = sc.BLOCK_SIZE_512

    # Tests also alignment and block size properties here.
    assert dom.alignment == sc.ALIGNMENT_1M
    assert dom.block_size == sc.BLOCK_SIZE_512

    actual = dom.getMetadata()

    assert expected == actual

    # Check that first PV is device where metadata is stored.
    assert dev1 == lvm.getVgMetadataPv(dom.sdUUID)

    lv = lvm.getLV(dom.sdUUID, sd.METADATA)
    assert int(lv.size) == blockSD.METADATA_LV_SIZE_MB * MiB

    # Test the domain lease.
    lease = dom.getClusterLease()
    assert lease.name == "SDM"
    assert lease.path == "/dev/{}/leases".format(dom.sdUUID)
    assert lease.offset == dom.alignment

    resource = fake_sanlock.read_resource(
        lease.path,
        lease.offset,
        align=dom.alignment,
        sector=dom.block_size)

    assert resource == {
        "acquired": False,
        "align": dom.alignment,
        "lockspace": dom.sdUUID.encode("utf-8"),
        "resource": lease.name.encode("utf-8"),
        "sector": dom.block_size,
        "version": 0,
    }

    # Test special volumes sizes.

    for name in (sd.IDS, sd.INBOX, sd.OUTBOX, sd.METADATA):
        lv = lvm.getLV(dom.sdUUID, name)
        # This is the minimal LV size on block storage.
        assert int(lv.size) == 128 * MiB

    lv = lvm.getLV(dom.sdUUID, blockSD.MASTERLV)
    assert int(lv.size) == GiB

    lv = lvm.getLV(dom.sdUUID, sd.LEASES)
    assert int(lv.size) == sd.LEASES_SLOTS * dom.alignment

    if domain_version > 3:
        lv = lvm.getLV(dom.sdUUID, sd.XLEASES)
        assert int(lv.size) == sd.XLEASES_SLOTS * dom.alignment


@requires_root
@pytest.mark.root
def test_create_instance_block_size_mismatch(
        tmp_storage, tmp_repo, fake_sanlock):
    sd_uuid = str(uuid.uuid4())

    dev = tmp_storage.create_device(10 * GiB)
    lvm.createVG(sd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
    vg = lvm.getVG(sd_uuid)

    dom = blockSD.BlockStorageDomain.create(
        sdUUID=sd_uuid,
        domainName="test",
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=5,
        storageType=sd.ISCSI_DOMAIN)

    # Change metadata to report the wrong block size for current storage.
    dom.setMetaParam(sd.DMDK_BLOCK_SIZE, sc.BLOCK_SIZE_4K)

    # Creating a new instance should fail now.
    with pytest.raises(se.StorageDomainBlockSizeMismatch):
        blockSD.BlockStorageDomain(sd_uuid)


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [3, 4, 5])
def test_volume_life_cycle(monkeypatch, tmp_storage, tmp_repo, fake_access,
                           fake_rescan, tmp_db, fake_task, fake_sanlock,
                           domain_version):
    # as creation of block storage domain and volume is quite time consuming,
    # we test several volume operations in one test to speed up the test suite

    sd_uuid = str(uuid.uuid4())
    domain_name = "domain"

    dev = tmp_storage.create_device(20 * GiB)
    lvm.createVG(sd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
    vg = lvm.getVG(sd_uuid)

    dom = blockSD.BlockStorageDomain.create(
        sdUUID=sd_uuid,
        domainName=domain_name,
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=domain_version,
        storageType=sd.ISCSI_DOMAIN)

    sdCache.knownSDs[sd_uuid] = blockSD.findDomain
    sdCache.manuallyAddDomain(dom)

    img_uuid = str(uuid.uuid4())
    vol_uuid = str(uuid.uuid4())
    vol_capacity = 10 * GiB
    vol_desc = "Test volume"

    # Create domain directory structure.
    dom.refresh()
    # Attache repo pool - SD expects at least one pool is attached.
    dom.attach(tmp_repo.pool_id)

    with monkeypatch.context() as mc:
        mc.setattr(time, "time", lambda: 1550522547)
        dom.createVolume(
            imgUUID=img_uuid,
            capacity=vol_capacity,
            volFormat=sc.COW_FORMAT,
            preallocate=sc.SPARSE_VOL,
            diskType=sc.DATA_DISKTYPE,
            volUUID=vol_uuid,
            desc=vol_desc,
            srcImgUUID=sc.BLANK_UUID,
            srcVolUUID=sc.BLANK_UUID)

    # test create volume
    vol = dom.produceVolume(img_uuid, vol_uuid)

    # Get the metadata slot, used for volume metadata and volume lease offset.
    _, slot = vol.getMetadataId()

    lease = dom.getVolumeLease(img_uuid, vol_uuid)

    assert lease.name == vol.volUUID
    assert lease.path == "/dev/{}/leases".format(sd_uuid)
    assert lease.offset == (sd.RESERVED_LEASES + slot) * dom.alignment

    # Test that we created a sanlock resource for this volume.
    resource = fake_sanlock.read_resource(
        lease.path,
        lease.offset,
        align=dom.alignment,
        sector=dom.block_size)

    assert resource == {
        "acquired": False,
        "align": dom.alignment,
        "lockspace": vol.sdUUID.encode("utf-8"),
        "resource": vol.volUUID.encode("utf-8"),
        "sector": dom.block_size,
        "version": 0,
    }

    # Test volume info.
    actual = vol.getInfo()

    assert int(actual["capacity"]) == vol_capacity
    assert int(actual["ctime"]) == 1550522547
    assert actual["description"] == vol_desc
    assert actual["disktype"] == "DATA"
    assert actual["domain"] == sd_uuid
    assert actual["format"] == "COW"
    assert actual["lease"] == {
        "offset": lease.offset,
        "owners": [],
        "path": lease.path,
        "version": None,
    }
    assert actual["parent"] == sc.BLANK_UUID
    assert actual["status"] == sc.VOL_STATUS_OK
    assert actual["type"] == "SPARSE"
    assert actual["voltype"] == "LEAF"
    assert actual["uuid"] == vol_uuid

    vol_path = vol.getVolumePath()

    # test volume prepare
    assert os.path.islink(vol_path)
    assert not os.path.exists(vol_path)

    lv_size = int(lvm.getLV(sd_uuid, vol_uuid).size)

    # Check volume size of unprepared volume - uses lvm.
    size = dom.getVolumeSize(img_uuid, vol_uuid)
    assert size.apparentsize == size.truesize == lv_size

    vol.prepare()

    assert os.path.exists(vol_path)

    # Check volume size of prepared volume - uses seek.
    size = dom.getVolumeSize(img_uuid, vol_uuid)
    assert size.apparentsize == size.truesize == lv_size

    # verify we can really write and read to an image
    qemuio.write_pattern(vol_path, "qcow2")
    qemuio.verify_pattern(vol_path, "qcow2")

    # test volume teardown
    vol.teardown(sd_uuid, vol_uuid)

    assert os.path.islink(vol_path)
    assert not os.path.exists(vol_path)

    # test also deleting of the volume
    vol.delete(postZero=False, force=False, discard=False)

    # verify lvm with volume is deleted
    assert not os.path.islink(vol.getVolumePath())
    with pytest.raises(se.LogicalVolumeDoesNotExistError):
        lvm.getLV(sd_uuid, vol_uuid)


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [4, 5])
def test_volume_metadata(tmp_storage, tmp_repo, fake_access, fake_rescan,
                         tmp_db, fake_task, fake_sanlock, domain_version):
    sd_uuid = str(uuid.uuid4())

    dev = tmp_storage.create_device(20 * GiB)
    lvm.createVG(sd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
    vg = lvm.getVG(sd_uuid)

    dom = blockSD.BlockStorageDomain.create(
        sdUUID=sd_uuid,
        domainName="domain",
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=domain_version,
        storageType=sd.ISCSI_DOMAIN)

    sdCache.knownSDs[sd_uuid] = blockSD.findDomain
    sdCache.manuallyAddDomain(dom)

    dom.refresh()
    dom.attach(tmp_repo.pool_id)

    img_uuid = str(uuid.uuid4())
    vol_uuid = str(uuid.uuid4())

    dom.createVolume(
        desc="old description",
        diskType="DATA",
        imgUUID=img_uuid,
        preallocate=sc.SPARSE_VOL,
        capacity=10 * GiB,
        srcImgUUID=sc.BLANK_UUID,
        srcVolUUID=sc.BLANK_UUID,
        volFormat=sc.COW_FORMAT,
        volUUID=vol_uuid)

    vol = dom.produceVolume(img_uuid, vol_uuid)

    # Test metadata offset
    _, slot = vol.getMetadataId()
    offset = dom.manifest.metadata_offset(slot)
    if domain_version < 5:
        assert offset == slot * blockSD.METADATA_SLOT_SIZE_V4
    else:
        assert offset == (blockSD.METADATA_BASE_V5 + slot *
                          blockSD.METADATA_SLOT_SIZE_V5)

    meta_path = dom.manifest.metadata_volume_path()

    # Check capacity
    assert 10 * GiB == vol.getCapacity()
    vol.setCapacity(0)
    with pytest.raises(se.MetaDataValidationError):
        vol.getCapacity()
    vol.setCapacity(10 * GiB)

    # Change metadata.
    md = vol.getMetadata()
    md.description = "new description"
    vol.setMetadata(md)
    with open(meta_path, "rb") as f:
        f.seek(offset)
        data = f.read(sc.METADATA_SIZE)
    data = data.rstrip(b"\0")
    assert data == md.storage_format(domain_version)

    # Add additioanl metadata.
    md = vol.getMetadata()
    vol.setMetadata(md, CAP=md.capacity)
    with open(meta_path, "rb") as f:
        f.seek(offset)
        data = f.read(sc.METADATA_SIZE)
    data = data.rstrip(b"\0")
    assert data == md.storage_format(domain_version, CAP=md.capacity)


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [4, 5])
def test_create_snapshot_size(
        tmp_storage, tmp_repo, fake_access, fake_rescan, tmp_db, fake_task,
        fake_sanlock, domain_version):
    # This test was added to verify fix for https://bugzilla.redhat.com/1700623
    # As a result of this bug, there can be volumes with corrupted metadata
    # capacity. The metadata of such volume should be fixed when the volume is
    # prepared. As the creation of tmp storage for block SD is time consuming,
    # let's test this flow also in this test.
    sd_uuid = str(uuid.uuid4())

    dev = tmp_storage.create_device(20 * GiB)
    lvm.createVG(sd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
    vg = lvm.getVG(sd_uuid)

    dom = blockSD.BlockStorageDomain.create(
        sdUUID=sd_uuid,
        domainName="domain",
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=domain_version,
        storageType=sd.ISCSI_DOMAIN)

    sdCache.knownSDs[sd_uuid] = blockSD.findDomain
    sdCache.manuallyAddDomain(dom)

    dom.refresh()
    dom.attach(tmp_repo.pool_id)

    img_uuid = str(uuid.uuid4())
    parent_vol_uuid = str(uuid.uuid4())
    parent_vol_capacity = GiB
    vol_uuid = str(uuid.uuid4())
    vol_capacity = 2 * parent_vol_capacity

    # Create parent volume.

    dom.createVolume(
        imgUUID=img_uuid,
        capacity=parent_vol_capacity,
        volFormat=sc.RAW_FORMAT,
        preallocate=sc.PREALLOCATED_VOL,
        diskType='DATA',
        volUUID=parent_vol_uuid,
        desc="Test parent volume",
        srcImgUUID=sc.BLANK_UUID,
        srcVolUUID=sc.BLANK_UUID)

    parent_vol = dom.produceVolume(img_uuid, parent_vol_uuid)

    # Verify that snapshot cannot be smaller than parent.
    # As we round capacity to 4k block size, we reduce it here by one 4k block.

    with pytest.raises(se.InvalidParameterException):
        dom.createVolume(
            imgUUID=img_uuid,
            capacity=parent_vol.getCapacity() - sc.BLOCK_SIZE_4K,
            volFormat=sc.COW_FORMAT,
            preallocate=sc.SPARSE_VOL,
            diskType='DATA',
            volUUID=vol_uuid,
            desc="Extended volume",
            srcImgUUID=parent_vol.imgUUID,
            srcVolUUID=parent_vol.volUUID)

    # Verify that snapshot can be bigger than parent.

    dom.createVolume(
        imgUUID=img_uuid,
        capacity=vol_capacity,
        volFormat=sc.COW_FORMAT,
        preallocate=sc.SPARSE_VOL,
        diskType='DATA',
        volUUID=vol_uuid,
        desc="Extended volume",
        srcImgUUID=parent_vol.imgUUID,
        srcVolUUID=parent_vol.volUUID)

    vol = dom.produceVolume(img_uuid, vol_uuid)

    # Verify volume sizes obtained from metadata
    actual_parent = parent_vol.getInfo()
    assert int(actual_parent["capacity"]) == parent_vol_capacity

    actual = vol.getInfo()
    assert int(actual["capacity"]) == vol_capacity

    # Now test the flow in which metadata capacity is corrupted.
    # Corrupt the metadata capacity manually.
    md = vol.getMetadata()
    md.capacity = vol_capacity // 2
    vol.setMetadata(md)

    # During preparation of the volume, matadata capacity should be fixed.
    vol.prepare()

    actual = vol.getInfo()
    assert int(actual["capacity"]) == vol_capacity

    # Corrupt the metadata capacity manually again.
    # And reuse this test also for testing Volume.syncMetadata().
    # As syncMetadata() work only for RAW volumes, test it on parent volume.
    parent_md = parent_vol.getMetadata()
    parent_md.capacity = parent_vol_capacity // 2
    parent_vol.setMetadata(md)

    parent_vol.syncMetadata()

    actual = parent_vol.getInfo()
    assert int(actual["capacity"]) == parent_vol_capacity


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [4, 5])
def test_create_with_bitmaps(
        tmp_storage, tmp_repo, fake_access, fake_rescan, tmp_db, fake_task,
        fake_sanlock, domain_version):
    sd_uuid = str(uuid.uuid4())

    dev = tmp_storage.create_device(20 * GiB)
    lvm.createVG(sd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
    vg = lvm.getVG(sd_uuid)

    dom = blockSD.BlockStorageDomain.create(
        sdUUID=sd_uuid,
        domainName="domain",
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=domain_version,
        storageType=sd.ISCSI_DOMAIN)

    sdCache.knownSDs[sd_uuid] = blockSD.findDomain
    sdCache.manuallyAddDomain(dom)

    dom.refresh()
    dom.attach(tmp_repo.pool_id)

    img_uuid = str(uuid.uuid4())
    base_vol_uuid = str(uuid.uuid4())
    base_vol_capacity = GiB
    top_vol_uuid = str(uuid.uuid4())
    vol_capacity = 2 * base_vol_capacity
    bitmap_names = ['bitmap1', 'bitmap2']

    # Create base volume.
    dom.createVolume(
        imgUUID=img_uuid,
        capacity=base_vol_capacity,
        volFormat=sc.COW_FORMAT,
        preallocate=sc.SPARSE_VOL,
        diskType='DATA',
        volUUID=base_vol_uuid,
        desc="Test base volume",
        srcImgUUID=sc.BLANK_UUID,
        srcVolUUID=sc.BLANK_UUID)

    base_vol = dom.produceVolume(img_uuid, base_vol_uuid)
    base_vol_path = base_vol.getVolumePath()

    # Prepare the volume in order to create bitmaps
    base_vol.prepare()
    # Add new bitmaps to base volume
    for bitmap_name in bitmap_names:
        op = qemuimg.bitmap_add(
            base_vol_path,
            bitmap_name,
        )
        op.run()

    # Teardown the volume, test if prepare() will be
    # called during the snapshot creation
    base_vol.teardown(sd_uuid, base_vol_uuid)

    # Create top volume with bitmaps.
    dom.createVolume(
        imgUUID=img_uuid,
        capacity=vol_capacity,
        volFormat=sc.COW_FORMAT,
        preallocate=sc.SPARSE_VOL,
        diskType='DATA',
        volUUID=top_vol_uuid,
        desc="Test top volume",
        srcImgUUID=base_vol.imgUUID,
        srcVolUUID=base_vol.volUUID,
        add_bitmaps=True)

    top_vol = dom.produceVolume(img_uuid, top_vol_uuid)
    top_vol_path = top_vol.getVolumePath()

    # Prepare the volume in order to get
    # info on the bitmaps
    top_vol.prepare()

    info = qemuimg.info(top_vol_path)

    # Teardown top volume
    base_vol.teardown(sd_uuid, top_vol_uuid)

    assert info['format-specific']['data']['bitmaps'] == [
        {
            "flags": ["auto"],
            "name": bitmap_names[0],
            "granularity": 65536
        },
        {
            "flags": ["auto"],
            "name": bitmap_names[1],
            "granularity": 65536
        },
    ]


@requires_root
@pytest.mark.root
def test_failed_to_add_bitmaps_to_v3_domain(
        tmp_storage, tmp_repo, fake_access, fake_rescan, tmp_db, fake_task,
        fake_sanlock):
    sd_uuid = str(uuid.uuid4())

    dev = tmp_storage.create_device(20 * GiB)
    lvm.createVG(sd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
    vg = lvm.getVG(sd_uuid)

    dom = blockSD.BlockStorageDomain.create(
        sdUUID=sd_uuid,
        domainName="domain",
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=3,
        storageType=sd.ISCSI_DOMAIN)

    sdCache.knownSDs[sd_uuid] = blockSD.findDomain
    sdCache.manuallyAddDomain(dom)

    dom.refresh()
    dom.attach(tmp_repo.pool_id)

    img_uuid = str(uuid.uuid4())
    base_vol_uuid = str(uuid.uuid4())
    base_vol_capacity = GiB
    top_vol_uuid = str(uuid.uuid4())
    vol_capacity = 2 * base_vol_capacity

    # Create base volume.
    dom.createVolume(
        imgUUID=img_uuid,
        capacity=base_vol_capacity,
        volFormat=sc.COW_FORMAT,
        preallocate=sc.SPARSE_VOL,
        diskType='DATA',
        volUUID=base_vol_uuid,
        desc="Test base volume",
        srcImgUUID=sc.BLANK_UUID,
        srcVolUUID=sc.BLANK_UUID)

    base_vol = dom.produceVolume(img_uuid, base_vol_uuid)
    with pytest.raises(se.UnsupportedOperation):
        # Create top volume with bitmaps.
        dom.createVolume(
            imgUUID=img_uuid,
            capacity=vol_capacity,
            volFormat=sc.COW_FORMAT,
            preallocate=sc.SPARSE_VOL,
            diskType='DATA',
            volUUID=top_vol_uuid,
            desc="Test top volume",
            srcImgUUID=base_vol.imgUUID,
            srcVolUUID=base_vol.volUUID,
            add_bitmaps=True)


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [4, 5])
def test_dump_sd_metadata(
        monkeypatch,
        tmp_storage,
        tmp_repo,
        fake_sanlock,
        fake_task,
        domain_version):

    # Allow to dump fake sanlock leases information created during the test.
    monkeypatch.setattr(
        sanlock_direct, "dump_leases", fake_sanlock.dump_leases)
    monkeypatch.setattr(
        sanlock_direct, "dump_lockspace", fake_sanlock.dump_lockspace)

    sd_uuid = str(uuid.uuid4())
    dev = tmp_storage.create_device(20 * GiB)
    lvm.createVG(sd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
    vg = lvm.getVG(sd_uuid)

    dom = blockSD.BlockStorageDomain.create(
        sdUUID=sd_uuid,
        domainName="test",
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=domain_version,
        storageType=sd.ISCSI_DOMAIN)
    dom.refresh()
    dom.attach(tmp_repo.pool_id)

    md_dev = os.path.basename(dev)
    expected_metadata = {
        'uuid': sd_uuid,
        'type': 'ISCSI',
        'class': 'Data',
        'name': 'test',
        'role': sd.REGULAR_DOMAIN,
        'pool': [tmp_repo.pool_id],
        'version': str(domain_version),
        'block_size': sc.BLOCK_SIZE_512,
        'alignment': sc.ALIGNMENT_1M,
        'vguuid': vg.uuid,
        'state': 'OK',
        'metadataDevice': md_dev,
        'vgMetadataDevice': md_dev
    }

    expected_sd_lease = {
        'offset': sc.ALIGNMENT_1M,
        'lockspace': sd_uuid,
        'resource': 'SDM',
        'timestamp': 0,
        'own': 0,
        'gen': 0,
        'lver': 0
    }

    # Real sanlock lockspace dump would have a unique host name for the
    # resource field and a valid lock creation timestamp, owner host id and
    # generation. For fake sanlock dump we only dump what fake sanlock
    # registers for test purpose.
    expected_lockspace = [{
        'offset': 0,
        'lockspace': sd_uuid,
        'resource': 0,
        'timestamp': 0,
        'own': 0,
        'gen': 0
    }]

    assert dom.dump() == {
        "metadata": expected_metadata,
        "volumes": {}
    }

    img_uuid = str(uuid.uuid4())
    vol_uuid = str(uuid.uuid4())
    vol_capacity = 10 * GiB
    vol_ctime = 1582196150

    with monkeypatch.context() as mc:
        mc.setattr(time, "time", lambda: vol_ctime)
        dom.createVolume(
            diskType=sc.DATA_DISKTYPE,
            imgUUID=img_uuid,
            preallocate=sc.SPARSE_VOL,
            desc="test",
            capacity=vol_capacity,
            srcImgUUID=sc.BLANK_UUID,
            srcVolUUID=sc.BLANK_UUID,
            volFormat=sc.COW_FORMAT,
            volUUID=vol_uuid,
            sequence=42)

    # Create external lease.
    dom.create_lease(vol_uuid)

    lease_info = dom._manifest.lease_info(vol_uuid)
    expected_xleases = {
        vol_uuid: {
            "offset": lease_info.offset,
            "updating": False
        }
    }

    expected_sequence = 42 if domain_version == 5 else sc.DEFAULT_SEQUENCE

    vol = dom.produceVolume(img_uuid, vol_uuid)
    mdslot = vol.getMetaSlot()
    vol_size = dom.getVolumeSize(img_uuid, vol_uuid)
    expected_volumes_metadata = {
        vol_uuid: {
            'apparentsize': vol_size.apparentsize,
            'capacity': vol_capacity,
            'ctime': vol_ctime,
            'description': 'test',
            'disktype': sc.DATA_DISKTYPE,
            'format': 'COW',
            'generation': 0,
            'sequence': expected_sequence,
            'image': img_uuid,
            'legality': sc.LEGAL_VOL,
            'mdslot': mdslot,
            'status': sc.VOL_STATUS_OK,
            'parent': sc.BLANK_UUID,
            'type': 'SPARSE',
            'voltype': 'LEAF',
            'truesize': vol_size.truesize
        }
    }

    expected_vol_lease = {
        'offset': (sd.RESERVED_LEASES + mdslot) * sc.ALIGNMENT_1M,
        'lockspace': sd_uuid,
        'resource': vol_uuid,
        'timestamp': 0,
        'own': 0,
        'gen': 0,
        'lver': 0
    }

    assert dom.dump(full=True) == {
        "metadata": expected_metadata,
        "volumes": expected_volumes_metadata,
        "leases": [expected_sd_lease, expected_vol_lease],
        "lockspace": expected_lockspace,
        "xleases": expected_xleases
    }

    assert dom.dump() == {
        "metadata": expected_metadata,
        "volumes": expected_volumes_metadata
    }

    # Uninitialized volume is excluded from dump.
    with change_vol_tag(vol, "", sc.TAG_VOL_UNINIT):
        assert dom.dump() == {
            "metadata": expected_metadata,
            "volumes": {}
        }

    # Tagged as removed volume is dumped with removed status.
    img_tag = sc.REMOVED_IMAGE_PREFIX + img_uuid
    with change_vol_tag(vol, sc.TAG_PREFIX_IMAGE, img_tag):
        assert dom.dump() == {
            "metadata": expected_metadata,
            "volumes": {
                vol_uuid: {
                    'apparentsize': vol_size.apparentsize,
                    'capacity': vol_capacity,
                    'ctime': vol_ctime,
                    'description': 'test',
                    'disktype': sc.DATA_DISKTYPE,
                    'format': 'COW',
                    'generation': 0,
                    'sequence': expected_sequence,
                    'image': img_uuid,
                    'legality': sc.LEGAL_VOL,
                    'mdslot': mdslot,
                    'status': sc.VOL_STATUS_REMOVED,
                    'parent': sc.BLANK_UUID,
                    'type': 'SPARSE',
                    'voltype': 'LEAF',
                    'truesize': vol_size.truesize
                }
            }
        }

    # Tagged as zeroed volume is dumped with removed status.
    img_tag = sc.ZEROED_IMAGE_PREFIX + img_uuid
    with change_vol_tag(vol, sc.TAG_PREFIX_IMAGE, img_tag):
        assert dom.dump() == {
            "metadata": expected_metadata,
            "volumes": {
                vol_uuid: {
                    'apparentsize': vol_size.apparentsize,
                    'capacity': vol_capacity,
                    'ctime': vol_ctime,
                    'description': 'test',
                    'disktype': sc.DATA_DISKTYPE,
                    'format': 'COW',
                    'generation': 0,
                    'sequence': expected_sequence,
                    'image': img_uuid,
                    'legality': sc.LEGAL_VOL,
                    'mdslot': mdslot,
                    'status': sc.VOL_STATUS_REMOVED,
                    'parent': sc.BLANK_UUID,
                    'type': 'SPARSE',
                    'voltype': 'LEAF',
                    'truesize': vol_size.truesize
                }
            }
        }

    # Bad MD slot tag volume will be dumped with invalid status.
    with change_vol_tag(vol, sc.TAG_PREFIX_MD, "bad-slot-number"):
        assert dom.dump() == {
            "metadata": expected_metadata,
            "volumes": {
                vol_uuid: {
                    "apparentsize": vol_size.apparentsize,
                    "image": img_uuid,
                    "status": sc.VOL_STATUS_INVALID,
                    "parent": sc.BLANK_UUID,
                    "truesize": vol_size.truesize
                }
            }
        }

    # Volume with error on getting size will be dumped with invalid status.
    with monkeypatch.context() as mc:
        def bad_vol_size():
            raise Exception()
        mc.setattr(
            blockSD.BlockStorageDomainManifest, "getVolumeSize", bad_vol_size)

        assert dom.dump() == {
            "metadata": expected_metadata,
            "volumes": {
                vol_uuid: {
                    'capacity': vol_capacity,
                    'ctime': vol_ctime,
                    'description': 'test',
                    'disktype': sc.DATA_DISKTYPE,
                    'format': 'COW',
                    'generation': 0,
                    'sequence': expected_sequence,
                    'image': img_uuid,
                    'legality': sc.LEGAL_VOL,
                    'mdslot': mdslot,
                    'status': sc.VOL_STATUS_INVALID,
                    'parent': sc.BLANK_UUID,
                    'type': 'SPARSE',
                    'voltype': 'LEAF'
                }
            }
        }

    # Remove volume metadata.
    vol.removeMetadata((sd_uuid, mdslot))

    # Metadata volume must be INVALID, but image uuid, parent uuid and mdslot
    # can reported from the lv tags.
    assert dom.dump() == {
        "metadata": expected_metadata,
        "volumes": {
            vol_uuid: {
                "apparentsize": vol_size.apparentsize,
                "image": img_uuid,
                "status": sc.VOL_STATUS_INVALID,
                "parent": sc.BLANK_UUID,
                "mdslot": mdslot,
                "truesize": vol_size.truesize,
                "generation": sc.DEFAULT_GENERATION,
                "sequence": sc.DEFAULT_SEQUENCE,
            }
        }
    }

    # If image tag is missing the image key is omitted.
    with delete_vol_tag(vol, sc.TAG_PREFIX_IMAGE):
        assert dom.dump() == {
            "metadata": expected_metadata,
            "volumes": {
                vol_uuid: {
                    "status": sc.VOL_STATUS_INVALID,
                    "parent": sc.BLANK_UUID,
                    "mdslot": mdslot,
                    "generation": sc.DEFAULT_GENERATION,
                    "sequence": sc.DEFAULT_SEQUENCE,
                }
            }
        }


@pytest.mark.parametrize("domain_version", [5])
def test_create_illegal_volume(domain_factory, domain_version, fake_task,
                               fake_sanlock):
    sd_uuid = str(uuid.uuid4())
    dom = domain_factory.create_domain(sd_uuid=sd_uuid, version=domain_version)

    img_uuid = str(uuid.uuid4())
    vol_id = str(uuid.uuid4())
    vol_capacity = 10 * GiB

    dom.createVolume(
        imgUUID=img_uuid,
        capacity=vol_capacity,
        volFormat=sc.COW_FORMAT,
        preallocate=sc.SPARSE_VOL,
        diskType=sc.DATA_DISKTYPE,
        volUUID=vol_id,
        desc="Base volume",
        srcImgUUID=sc.BLANK_UUID,
        srcVolUUID=sc.BLANK_UUID,
        legal=False)

    vol = dom.produceVolume(img_uuid, vol_id)

    assert vol.getLegality() == sc.ILLEGAL_VOL


LVM_TAG_CHARS = string.ascii_letters + "0123456789_+.-/=!:#"

LVM_TAGS = [
    LVM_TAG_CHARS,
    u"&",
    u"\u05d9",
    LVM_TAG_CHARS + "$@|",
]

ENCODED_LVM_TAGS = [
    LVM_TAG_CHARS,
    u"&38&",
    u"&1497&",
    LVM_TAG_CHARS + "&36&&64&&124&",
]

TEST_IDS = [
    "lvm_tag_chars",
    "ampersand",
    "unicode_char",
    "lvm_tag_chars_with_symbols",
]


@pytest.mark.parametrize("lvm_tag", LVM_TAGS, ids=TEST_IDS)
def test_lvmtag_roundtrip(lvm_tag):
    assert blockSD.lvmTagDecode(blockSD.lvmTagEncode(lvm_tag)) == lvm_tag


@pytest.mark.parametrize(
    "encoded_tag,lvm_tag",
    list(zip(ENCODED_LVM_TAGS, LVM_TAGS)),
    ids=TEST_IDS)
def test_lvmtag_decode(encoded_tag, lvm_tag):
    assert blockSD.lvmTagDecode(encoded_tag) == lvm_tag


@pytest.mark.parametrize(
    "lvm_tag,encoded_tag",
    list(zip(LVM_TAGS, ENCODED_LVM_TAGS)),
    ids=TEST_IDS)
def test_lvmtag_encode(lvm_tag, encoded_tag):
    assert blockSD.lvmTagEncode(lvm_tag) == encoded_tag


@requires_root
@pytest.mark.root
def test_spm_lifecycle(
        tmp_storage,
        tmp_repo,
        fake_access,
        fake_rescan,
        tmp_db,
        fake_task,
        fake_sanlock):

    msd_uuid = str(uuid.uuid4())

    dev = tmp_storage.create_device(20 * GiB)
    lvm.createVG(msd_uuid, [dev], blockSD.STORAGE_UNREADY_DOMAIN_TAG, 128)
    vg = lvm.getVG(msd_uuid)

    master_dom = blockSD.BlockStorageDomain.create(
        sdUUID=msd_uuid,
        domainName="domain",
        domClass=sd.DATA_DOMAIN,
        vgUUID=vg.uuid,
        version=5,
        storageType=sd.ISCSI_DOMAIN)

    sdCache.knownSDs[msd_uuid] = blockSD.findDomain
    sdCache.manuallyAddDomain(master_dom)

    pool = sp.StoragePool(
        tmp_repo.pool_id,
        FakeDomainMonitor(),
        FakeTaskManager())
    pool.setBackend(StoragePoolDiskBackend(pool))
    pool.create(
        poolName="pool",
        msdUUID=msd_uuid,
        domList=[msd_uuid],
        masterVersion=0,
        leaseParams=sd.DEFAULT_LEASE_PARAMS)

    pool.startSpm(prevID=0, prevLVER=0, maxHostID=clusterlock.MAX_HOST_ID)
    pool.stopSpm()


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [5])
def test_sync_volume_chain_active(
        domain_factory,
        fake_task,
        fake_sanlock,
        domain_version):

    sd_uuid = str(uuid.uuid4())
    dom = domain_factory.create_domain(sd_uuid=sd_uuid, version=domain_version)
    chain = create_chain(dom)

    assert chain.top.isLegal()

    # Simulate top/leaf volume removal from actual chain.
    actual_chain = [chain.base.volUUID, chain.internal.volUUID]

    # Sync volume chain - in libvirt this would happen during snapshot removal
    # when the volume being removed *is* the active layer and pivot is
    # performed. Expected result after calling the volume sync is that the top
    # volume *is* put into ILLEGAL state and the chain shifts it's parent
    # metadata to reflect the new chain after removal. Purpose of syncing the
    # volume chain is to modify current(vdsm) chain according to
    # actual(libvirt) chain.
    img = image.Image(chain.top.repoPath)
    img.syncVolumeChain(
        sd_uuid, chain.top.imgUUID, chain.top.volUUID, actual_chain)

    # Verify top volume is illegal now.
    assert not chain.top.isLegal()

    # Verify current chain parents did not change.
    assert chain.base.getParent() == sc.BLANK_UUID
    assert chain.internal.getParent() == chain.base.volUUID
    assert chain.top.getParent() == chain.internal.volUUID


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [5])
def test_sync_volume_chain_internal(
        domain_factory,
        fake_task,
        fake_sanlock,
        domain_version):

    sd_uuid = str(uuid.uuid4())
    dom = domain_factory.create_domain(sd_uuid=sd_uuid, version=domain_version)
    chain = create_chain(dom)

    assert chain.top.isLegal()

    # Simulate internal volume removal from actual chain.
    actual_chain = [chain.base.volUUID, chain.top.volUUID]

    # Sync volume chain - in libvirt this would happen during snapshot removal
    # when the volume being removed is *not* the active layer. Expected result
    # after calling the volume sync is that the top volume is *not* put into
    # ILLEGAL state and the chain shifts it's parent metadata to reflect the
    # new chain after removal. Purpose of syncing the volume chain is to
    # modify current(vdsm) chain according to actual(libvirt) chain.
    img = image.Image(chain.top.repoPath)
    img.syncVolumeChain(
        sd_uuid, chain.top.imgUUID, chain.top.volUUID, actual_chain)

    # Verify top volume is still legal.
    assert chain.top.isLegal()

    # Verify that base volume parent did not change.
    assert chain.base.getParent() == sc.BLANK_UUID

    # Because imageSyncVolumeChain runs on any host, it cannot chang the lv
    # tags, only the volume metadata. getParentMeta() returns the parent UUID
    # from the volume metadata.
    assert chain.top.getParentMeta() == chain.base.volUUID

    # getParent() uses the lv tags, so it still returns the internal volume.
    # This will be fixed later on the SPM when the internal volume is deleted.
    assert chain.top.getParent() == chain.internal.volUUID


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [5])
def test_sync_volume_chain_recovery(
        domain_factory,
        fake_task,
        fake_sanlock,
        domain_version):

    sd_uuid = str(uuid.uuid4())
    dom = domain_factory.create_domain(sd_uuid=sd_uuid, version=domain_version)
    chain = create_chain(dom)

    assert chain.top.isLegal()

    # Simulate top/leaf volume removal from actual chain.
    actual_chain = [chain.base.volUUID, chain.internal.volUUID]

    # Sync volume chain.
    img = image.Image(chain.top.repoPath)
    img.syncVolumeChain(
        sd_uuid, chain.top.imgUUID, chain.top.volUUID, actual_chain)

    # Simulate recovery - actual chain is unchanged and sync called again.
    # This should happen when libvirt failed during pivot and we're trying to
    # recover. If the requested and current chain matches and top volume
    # is currently ILLEGAL it should change back to LEGAL.
    actual_chain = [
        chain.base.volUUID, chain.internal.volUUID, chain.top.volUUID]

    img = image.Image(chain.top.repoPath)
    img.syncVolumeChain(
        sd_uuid, chain.top.imgUUID, chain.top.volUUID, actual_chain)

    # Verify top volume was recovered and is legal now.
    assert chain.top.isLegal()

    current_chain = [vol.volUUID
                     for vol in img.getChain(sd_uuid, chain.top.imgUUID)]

    # Verify requested/actual chain matches current chain.
    assert current_chain == actual_chain


@requires_root
@pytest.mark.root
@pytest.mark.parametrize("domain_version", [5])
def test_sync_volume_chain_noop(
        domain_factory,
        fake_task,
        fake_sanlock,
        domain_version):

    sd_uuid = str(uuid.uuid4())
    dom = domain_factory.create_domain(sd_uuid=sd_uuid, version=domain_version)
    chain = create_chain(dom)

    assert chain.top.isLegal()

    # Simulate a case when actual chain is the same as current chain.
    actual_chain = [
        chain.base.volUUID, chain.internal.volUUID, chain.top.volUUID]

    # Sync volume chain - no change will be done.
    img = image.Image(chain.top.repoPath)
    img.syncVolumeChain(
        sd_uuid, chain.top.imgUUID, chain.top.volUUID, actual_chain)

    # Verify top volume is unchanged and still marked legal.
    assert chain.top.isLegal()

    current_chain = [vol.volUUID
                     for vol in img.getChain(sd_uuid, chain.top.imgUUID)]

    # Verify requested/actual chain matches current chain.
    assert current_chain == actual_chain
