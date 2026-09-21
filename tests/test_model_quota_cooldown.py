import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from src.core.database import Database
from src.core.models import Token
from src.services.generation_handler import GenerationHandler
from src.services.load_balancer import LoadBalancer


def make_token(token_id: int) -> Token:
    return Token(
        id=token_id,
        st=f"st-{token_id}",
        at=f"at-{token_id}",
        email=f"account-{token_id}@example.test",
        user_paygate_tier="PAYGATE_TIER_ONE",
        image_enabled=True,
        video_enabled=True,
    )


class StubTokenManager:
    def __init__(self, tokens, cooled_by_model=None):
        self.tokens = list(tokens)
        self.cooled_by_model = cooled_by_model or {}

    async def get_active_tokens(self):
        return list(self.tokens)

    async def get_token_ids_in_model_cooldown(self, model_key):
        return set(self.cooled_by_model.get(model_key, set()))

    def needs_at_refresh(self, token):
        return False

    async def ensure_valid_token(self, token):
        return token


class RecordingTokenManager:
    def __init__(self):
        self.cooldowns = []
        self.errors = []

    async def cool_down_model_daily_quota(self, token_id, model_key):
        self.cooldowns.append((token_id, model_key))
        return datetime(2030, 1, 1, tzinfo=timezone.utc)

    async def cool_down_model_transient_throttle(self, token_id, model_key):
        self.cooldowns.append((token_id, model_key))
        return datetime(2030, 1, 1, tzinfo=timezone.utc)

    async def record_error(self, token_id):
        self.errors.append(token_id)


class ModelQuotaCooldownDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        await self.db.init_db()
        self.token_id = await self.db.add_token(make_token(1).model_copy(update={"id": None}))

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    async def test_model_cooldown_is_persistent_scoped_and_expires(self):
        await self.db.set_token_model_cooldown(
            self.token_id,
            "gem_pix_2",
            "daily_quota",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )

        self.assertEqual(
            await self.db.get_token_ids_in_model_cooldown("GEM_PIX_2"),
            {self.token_id},
        )
        self.assertEqual(
            await self.db.get_token_ids_in_model_cooldown("NARWHAL"),
            set(),
        )

        await self.db.set_token_model_cooldown(
            self.token_id,
            "GEM_PIX_2",
            "daily_quota",
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        self.assertEqual(
            await self.db.get_token_ids_in_model_cooldown("GEM_PIX_2"),
            set(),
        )


class ModelQuotaCooldownSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_filters_only_the_exhausted_model_family(self):
        token = make_token(1)
        manager = StubTokenManager(
            [token],
            cooled_by_model={"GEM_PIX_2": {1}},
        )
        balancer = LoadBalancer(manager)

        with patch.object(
            balancer,
            "_check_extension_route",
            AsyncMock(return_value=(True, "")),
        ):
            exhausted = await balancer.select_token(
                for_image_generation=True,
                model="gemini-3.0-pro-image-landscape",
                model_quota_key="GEM_PIX_2",
                enforce_concurrency_filter=False,
            )
            available = await balancer.select_token(
                for_image_generation=True,
                model="gemini-3.1-flash-image-landscape",
                model_quota_key="NARWHAL",
                enforce_concurrency_filter=False,
            )

        self.assertIsNone(exhausted)
        self.assertEqual(available.id, 1)

    async def test_exhausted_account_is_skipped_for_same_model_family(self):
        manager = StubTokenManager(
            [make_token(1), make_token(2)],
            cooled_by_model={"GEM_PIX_2": {1}},
        )
        balancer = LoadBalancer(manager)

        with patch.object(
            balancer,
            "_check_extension_route",
            AsyncMock(return_value=(True, "")),
        ):
            selected = await balancer.select_token(
                for_image_generation=True,
                model="gemini-3.0-pro-image-landscape-2k",
                model_quota_key="GEM_PIX_2",
                enforce_concurrency_filter=False,
            )

        self.assertEqual(selected.id, 2)

    def test_daily_quota_classifier_is_exact(self):
        self.assertTrue(
            GenerationHandler._is_model_daily_quota_error(
                "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED: Quota exceeded"
            )
        )
        self.assertFalse(
            GenerationHandler._is_model_daily_quota_error(
                "PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"
            )
        )
        self.assertTrue(
            GenerationHandler._is_model_transient_throttle_error(
                "PUBLIC_ERROR_USER_REQUESTS_THROTTLED: Slow down"
            )
        )

    async def test_daily_quota_failure_does_not_increment_account_breaker(self):
        manager = RecordingTokenManager()
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.token_manager = manager

        await handler._record_token_failure(
            7,
            "GEM_PIX_2",
            "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED: Quota exceeded",
        )

        self.assertEqual(manager.cooldowns, [(7, "GEM_PIX_2")])
        self.assertEqual(manager.errors, [])

    async def test_user_throttle_uses_short_model_cooldown_not_account_breaker(self):
        manager = RecordingTokenManager()
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.token_manager = manager

        await handler._record_token_failure(
            10,
            "NARWHAL",
            "PUBLIC_ERROR_USER_REQUESTS_THROTTLED: Slow down",
        )

        self.assertEqual(manager.cooldowns, [(10, "NARWHAL")])
        self.assertEqual(manager.errors, [])


if __name__ == "__main__":
    unittest.main()
