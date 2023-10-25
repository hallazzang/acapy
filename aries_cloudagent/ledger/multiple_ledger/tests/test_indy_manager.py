import asyncio
from copy import deepcopy
import pytest
import json

from unittest import IsolatedAsyncioTestCase
from unittest import mock as async_mock

from collections import OrderedDict

from ....cache.base import BaseCache
from ....cache.in_memory import InMemoryCache
from ....core.in_memory import InMemoryProfile
from ....ledger.base import BaseLedger
from ....messaging.responder import BaseResponder

from ...error import LedgerError
from ...indy import IndySdkLedger, IndySdkLedgerPool
from ...merkel_validation.tests.test_data import GET_NYM_REPLY

from .. import indy_manager as test_module
from ..base_manager import MultipleLedgerManagerError
from ..indy_manager import MultiIndyLedgerManager


@pytest.mark.indy
class TestMultiIndyLedgerManager(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.profile = InMemoryProfile.test_profile(bind={BaseCache: InMemoryCache()})
        self.context = self.profile.context
        setattr(self.context, "profile", self.profile)
        self.responder = async_mock.AsyncMock(send=async_mock.AsyncMock())
        self.context.injector.bind_instance(BaseResponder, self.responder)
        self.production_ledger = OrderedDict()
        self.non_production_ledger = OrderedDict()
        test_prod_ledger = IndySdkLedger(
            IndySdkLedgerPool("test_prod_1", checked=True), self.profile
        )
        writable_ledgers = set()
        self.context.injector.bind_instance(BaseLedger, test_prod_ledger)
        self.production_ledger["test_prod_1"] = test_prod_ledger
        self.production_ledger["test_prod_2"] = IndySdkLedger(
            IndySdkLedgerPool("test_prod_2", checked=True), self.profile
        )
        self.non_production_ledger["test_non_prod_1"] = IndySdkLedger(
            IndySdkLedgerPool("test_non_prod_1", checked=True), self.profile
        )
        self.non_production_ledger["test_non_prod_2"] = IndySdkLedger(
            IndySdkLedgerPool("test_non_prod_2", checked=True), self.profile
        )
        writable_ledgers.add("test_prod_1")
        writable_ledgers.add("test_prod_2")
        self.manager = MultiIndyLedgerManager(
            self.profile,
            production_ledgers=self.production_ledger,
            non_production_ledgers=self.non_production_ledger,
            writable_ledgers=writable_ledgers,
        )

    def test_get_endorser_info_for_ledger(self):
        writable_ledgers = set()
        writable_ledgers.add("test_prod_1")
        writable_ledgers.add("test_prod_2")

        endorser_info_map = {}
        endorser_info_map["test_prod_1"] = {
            "endorser_did": "test_public_did_1",
            "endorser_alias": "endorser_1",
        }
        endorser_info_map["test_prod_2"] = {
            "endorser_did": "test_public_did_2",
            "endorser_alias": "endorser_2",
        }
        manager = MultiIndyLedgerManager(
            self.profile,
            production_ledgers=self.production_ledger,
            non_production_ledgers=self.non_production_ledger,
            writable_ledgers=writable_ledgers,
            endorser_map=endorser_info_map,
        )
        assert (
            "endorser_1"
        ), "test_public_did_1" == manager.get_endorser_info_for_ledger("test_prod_1")
        assert (
            "endorser_2"
        ), "test_public_did_2" == manager.get_endorser_info_for_ledger("test_prod_2")

    async def test_get_write_ledgers(self):
        ledger_ids = await self.manager.get_write_ledgers()
        assert "test_prod_1" in ledger_ids
        assert "test_prod_2" in ledger_ids

    async def test_get_write_ledger_from_base_ledger(self):
        ledger_id = await self.manager.get_ledger_id_by_ledger_pool_name("test_prod_1")
        assert ledger_id == "test_prod_1"

    async def test_set_profile_write_ledger(self):
        writable_ledgers = set()
        writable_ledgers.add("test_prod_1")
        writable_ledgers.add("test_prod_2")
        endorser_info_map = {}
        endorser_info_map["test_prod_2"] = {
            "endorser_did": "test_public_did_2",
            "endorser_alias": "endorser_2",
        }
        manager = MultiIndyLedgerManager(
            self.profile,
            production_ledgers=self.production_ledger,
            non_production_ledgers=self.non_production_ledger,
            writable_ledgers=writable_ledgers,
            endorser_map=endorser_info_map,
        )
        profile = InMemoryProfile.test_profile()
        assert not profile.inject_or(BaseLedger)
        assert "test_prod_2" in manager.writable_ledgers
        new_write_ledger_id = await manager.set_profile_write_ledger(
            profile=profile, ledger_id="test_prod_2"
        )
        assert new_write_ledger_id == "test_prod_2"
        new_write_ledger = profile.inject_or(BaseLedger)
        assert new_write_ledger.pool_name == "test_prod_2"

    async def test_set_profile_write_ledger_x(self):
        profile = InMemoryProfile.test_profile()
        with self.assertRaises(MultipleLedgerManagerError) as cm:
            new_write_ledger_id = await self.manager.set_profile_write_ledger(
                profile=profile, ledger_id="test_non_prod_1"
            )
        assert "is not write configurable" in str(cm.exception.message)

    async def test_get_ledger_inst_by_id(self):
        ledger_inst = await self.manager.get_ledger_inst_by_id("test_prod_2")
        assert ledger_inst
        ledger_inst = await self.manager.get_ledger_inst_by_id("test_non_prod_2")
        assert ledger_inst
        ledger_inst = await self.manager.get_ledger_inst_by_id("test_invalid")
        assert not ledger_inst

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_ledger_by_did_self_cert_a(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(GET_NYM_REPLY)
            mock_wait.return_value = mock_submit.return_value
            (
                ledger_id,
                ledger_inst,
                is_self_certified,
            ) = await self.manager._get_ledger_by_did(
                "test_prod_1", "Av63wJYM7xYR4AiygYq4c3"
            )
            assert ledger_id == "test_prod_1"
            assert ledger_inst.pool.name == "test_prod_1"
            assert is_self_certified

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_ledger_by_did_self_cert_b(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        self.non_production_ledger = OrderedDict()
        self.non_production_ledger["test_non_prod_1"] = IndySdkLedger(
            IndySdkLedgerPool("test_non_prod_1", checked=True), self.profile
        )
        self.non_production_ledger["test_non_prod_2"] = IndySdkLedger(
            IndySdkLedgerPool("test_non_prod_2", checked=True), self.profile
        )
        self.manager = MultiIndyLedgerManager(
            self.profile,
            non_production_ledgers=self.non_production_ledger,
        )
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(GET_NYM_REPLY)
            mock_wait.return_value = mock_submit.return_value
            (
                ledger_id,
                ledger_inst,
                is_self_certified,
            ) = await self.manager._get_ledger_by_did(
                "test_non_prod_1", "Av63wJYM7xYR4AiygYq4c3"
            )
            assert ledger_id == "test_non_prod_1"
            assert ledger_inst.pool.name == "test_non_prod_1"
            assert is_self_certified

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_ledger_by_did_not_self_cert(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        get_nym_reply = deepcopy(GET_NYM_REPLY)
        get_nym_reply["result"]["data"] = json.dumps(
            {
                "dest": "Av63wJYM7xYR4AiygYq4c3",
                "identifier": "V4SGRU86Z58d6TV7PBUe6f",
                "role": "101",
                "seqNo": 17794,
                "txnTime": 1632262244,
                "verkey": "ABUF7uxYTxZ6qYdZ4G9e1Gi",
            }
        )
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait, async_mock.patch.object(
            test_module.SubTrie, "verify_spv_proof", async_mock.AsyncMock()
        ) as mock_verify_spv_proof:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(get_nym_reply)
            mock_wait.return_value = mock_submit.return_value
            mock_verify_spv_proof.return_value = True
            (
                ledger_id,
                ledger_inst,
                is_self_certified,
            ) = await self.manager._get_ledger_by_did(
                "test_prod_1", "Av63wJYM7xYR4AiygYq4c3"
            )
            assert ledger_id == "test_prod_1"
            assert ledger_inst.pool.name == "test_prod_1"
            assert not is_self_certified

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_ledger_by_did_state_proof_not_valid(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        get_nym_reply = deepcopy(GET_NYM_REPLY)
        get_nym_reply["result"]["data"]["verkey"] = "ABUF7uxYTxZ6qYdZ4G9e1Gi"
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(get_nym_reply)
            mock_wait.return_value = mock_submit.return_value
            assert not await self.manager._get_ledger_by_did(
                "test_prod_1", "Av63wJYM7xYR4AiygYq4c3"
            )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_ledger_by_did_no_data(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        get_nym_reply = deepcopy(GET_NYM_REPLY)
        get_nym_reply.get("result").pop("data")
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(get_nym_reply)
            mock_wait.return_value = mock_submit.return_value
            assert not await self.manager._get_ledger_by_did(
                "test_prod_1", "Av63wJYM7xYR4AiygYq4c3"
            )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_ledger_by_did_timeout(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        mock_build_get_nym_req.return_value = async_mock.MagicMock()
        mock_submit.side_effect = asyncio.TimeoutError
        assert not await self.manager._get_ledger_by_did(
            "test_prod_1", "Av63wJYM7xYR4AiygYq4c3"
        )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_ledger_by_did_ledger_error(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        mock_build_get_nym_req.return_value = async_mock.MagicMock()
        mock_submit.side_effect = LedgerError
        assert not await self.manager._get_ledger_by_did(
            "test_prod_1", "Av63wJYM7xYR4AiygYq4c3"
        )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_lookup_did_in_configured_ledgers_self_cert_prod(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(GET_NYM_REPLY)
            mock_wait.return_value = mock_submit.return_value
            (
                ledger_id,
                ledger_inst,
            ) = await self.manager.lookup_did_in_configured_ledgers(
                "Av63wJYM7xYR4AiygYq4c3", cache_did=True
            )
            assert ledger_id == "test_prod_1"
            assert ledger_inst.pool.name == "test_prod_1"

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_ledger_by_did_not_self_cert_not_self_cert_prod(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        get_nym_reply = deepcopy(GET_NYM_REPLY)
        get_nym_reply["result"]["data"]["verkey"] = "ABUF7uxYTxZ6qYdZ4G9e1Gi"
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait, async_mock.patch.object(
            test_module.SubTrie, "verify_spv_proof", async_mock.AsyncMock()
        ) as mock_verify_spv_proof:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(get_nym_reply)
            mock_wait.return_value = mock_submit.return_value
            mock_verify_spv_proof.return_value = True
            (
                ledger_id,
                ledger_inst,
            ) = await self.manager.lookup_did_in_configured_ledgers(
                "Av63wJYM7xYR4AiygYq4c3", cache_did=True
            )
            assert ledger_id == "test_prod_1"
            assert ledger_inst.pool.name == "test_prod_1"

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_lookup_did_in_configured_ledgers_self_cert_non_prod(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        self.non_production_ledger = OrderedDict()
        self.non_production_ledger["test_non_prod_1"] = IndySdkLedger(
            IndySdkLedgerPool("test_non_prod_1", checked=True), self.profile
        )
        self.non_production_ledger["test_non_prod_2"] = IndySdkLedger(
            IndySdkLedgerPool("test_non_prod_2", checked=True), self.profile
        )
        self.manager = MultiIndyLedgerManager(
            self.profile,
            non_production_ledgers=self.non_production_ledger,
        )
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(GET_NYM_REPLY)
            mock_wait.return_value = mock_submit.return_value
            (
                ledger_id,
                ledger_inst,
            ) = await self.manager.lookup_did_in_configured_ledgers(
                "Av63wJYM7xYR4AiygYq4c3", cache_did=True
            )
            assert ledger_id == "test_non_prod_1"
            assert ledger_inst.pool.name == "test_non_prod_1"

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_ledger_by_did_not_self_cert_not_self_cert_non_prod(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        self.non_production_ledger = OrderedDict()
        self.non_production_ledger["test_non_prod_1"] = IndySdkLedger(
            IndySdkLedgerPool("test_non_prod_1", checked=True), self.profile
        )
        self.non_production_ledger["test_non_prod_2"] = IndySdkLedger(
            IndySdkLedgerPool("test_non_prod_2", checked=True), self.profile
        )
        self.manager = MultiIndyLedgerManager(
            self.profile,
            non_production_ledgers=self.non_production_ledger,
        )
        get_nym_reply = deepcopy(GET_NYM_REPLY)
        get_nym_reply["result"]["data"]["verkey"] = "ABUF7uxYTxZ6qYdZ4G9e1Gi"
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait, async_mock.patch.object(
            test_module.SubTrie, "verify_spv_proof", async_mock.AsyncMock()
        ) as mock_verify_spv_proof:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(get_nym_reply)
            mock_wait.return_value = mock_submit.return_value
            mock_verify_spv_proof.return_value = True
            (
                ledger_id,
                ledger_inst,
            ) = await self.manager.lookup_did_in_configured_ledgers(
                "Av63wJYM7xYR4AiygYq4c3", cache_did=True
            )
            assert ledger_id == "test_non_prod_1"
            assert ledger_inst.pool.name == "test_non_prod_1"

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_lookup_did_in_configured_ledgers_x(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait, async_mock.patch.object(
            test_module.SubTrie, "verify_spv_proof", async_mock.AsyncMock()
        ) as mock_verify_spv_proof:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(GET_NYM_REPLY)
            mock_wait.return_value = mock_submit.return_value
            mock_verify_spv_proof.return_value = False
            with self.assertRaises(MultipleLedgerManagerError) as cm:
                await self.manager.lookup_did_in_configured_ledgers(
                    "Av63wJYM7xYR4AiygYq4c3", cache_did=True
                )
                assert "not found in any of the ledgers total: (production: " in cm

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_lookup_did_in_configured_ledgers_prod_not_cached(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        with async_mock.patch.object(
            test_module.asyncio, "wait", async_mock.AsyncMock()
        ) as mock_wait:
            mock_build_get_nym_req.return_value = async_mock.MagicMock()
            mock_submit.return_value = json.dumps(GET_NYM_REPLY)
            mock_wait.return_value = mock_submit.return_value
            (
                ledger_id,
                ledger_inst,
            ) = await self.manager.lookup_did_in_configured_ledgers(
                "Av63wJYM7xYR4AiygYq4c3", cache_did=False
            )
            assert ledger_id == "test_prod_1"
            assert ledger_inst.pool.name == "test_prod_1"

    async def test_lookup_did_in_configured_ledgers_cached_prod_ledger(self):
        cache = InMemoryCache()
        await cache.set("did_ledger_id_resolver::Av63wJYM7xYR4AiygYq4c3", "test_prod_2")
        self.profile.context.injector.bind_instance(BaseCache, cache)
        (
            ledger_id,
            ledger_inst,
        ) = await self.manager.lookup_did_in_configured_ledgers(
            "Av63wJYM7xYR4AiygYq4c3", cache_did=True
        )
        assert ledger_id == "test_prod_2"
        assert ledger_inst.pool.name == "test_prod_2"

    async def test_lookup_did_in_configured_ledgers_cached_non_prod_ledger(self):
        cache = InMemoryCache()
        await cache.set(
            "did_ledger_id_resolver::Av63wJYM7xYR4AiygYq4c3", "test_non_prod_2", None
        )
        self.profile.context.injector.bind_instance(BaseCache, cache)
        (
            ledger_id,
            ledger_inst,
        ) = await self.manager.lookup_did_in_configured_ledgers(
            "Av63wJYM7xYR4AiygYq4c3", cache_did=True
        )
        assert ledger_id == "test_non_prod_2"
        assert ledger_inst.pool.name == "test_non_prod_2"

    async def test_lookup_did_in_configured_ledgers_cached_x(self):
        cache = InMemoryCache()
        await cache.set("did_ledger_id_resolver::Av63wJYM7xYR4AiygYq4c3", "invalid_id")
        self.profile.context.injector.bind_instance(BaseCache, cache)
        with self.assertRaises(MultipleLedgerManagerError) as cm:
            await self.manager.lookup_did_in_configured_ledgers(
                "Av63wJYM7xYR4AiygYq4c3", cache_did=True
            )
            assert "cached ledger_id invalid_id not found in either" in cm

    def test_extract_did_from_identifier(self):
        assert (
            self.manager.extract_did_from_identifier(
                "WgWxqztrNooG92RXvxSTWv:2:schema_name:1.0"
            )
            == "WgWxqztrNooG92RXvxSTWv"
        )
        assert (
            self.manager.extract_did_from_identifier(
                "WgWxqztrNooG92RXvxSTWv:3:CL:20:tag"
            )
            == "WgWxqztrNooG92RXvxSTWv"
        )
        assert (
            self.manager.extract_did_from_identifier("WgWxqztrNooG92RXvxSTWv")
            == "WgWxqztrNooG92RXvxSTWv"
        )
        assert (
            self.manager.extract_did_from_identifier("did:sov:WgWxqztrNooG92RXvxSTWv")
            == "WgWxqztrNooG92RXvxSTWv"
        )

    async def test_get_production_ledgers(self):
        assert len(await self.manager.get_prod_ledgers()) == 2

    async def test_get_non_production_ledgers(self):
        assert len(await self.manager.get_nonprod_ledgers()) == 2
