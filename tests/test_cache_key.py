"""A provider limit that only began to bind when a value moved.

`prompt_cache_key` is capped at 64 characters. Nothing was near it while a
project's identity was a slug, well under 20 characters. Moving the config
into the repository it describes made `work_dir` the identity, the identity a
path, and the key 98 characters. The reviewer's first call of the first run on
the new layout came back 400, and was then retried twice on a schedule meant
for dropped connections, at two and three minutes.

`executor.py` had already met this and answered with `[:64]` at its own call
site. The reviewer's site never got the same treatment, which is the failure
this file is really about: one fix, two places, and the second one found by a
live run.
"""

from code_gantry.cachekey import MAX_CACHE_KEY, cache_key


class TestItFits:
    def test_a_long_identity_is_bounded(self):
        # The real one, which is what produced the 400.
        identity = (
            "/Users/someone/projects/company/product/docs/"
            "a_long_migration_project_name/.code_gantry"
        )
        assert len(f"code_gantry:{identity}") > MAX_CACHE_KEY
        assert len(cache_key("code_gantry", identity)) <= MAX_CACHE_KEY

    def test_any_identity_at_all_fits(self):
        assert len(cache_key("code_gantry", "x" * 5_000)) <= MAX_CACHE_KEY


class TestItStaysTheSameKey:
    def test_an_identity_that_already_fits_is_untouched(self):
        """No working cache is invalidated by this existing.

        The executor's key is `exec:<branch>` and fits comfortably. If this
        rewrote it, shipping the fix would throw away a warm cache to solve a
        problem that role did not have.
        """
        assert cache_key("exec", "upgrade/rails-5") == "exec:upgrade/rails-5"

    def test_the_same_identity_gives_the_same_key(self):
        long = "/a/very/long/path/that/will/not/fit/inside/the/limit/at/all/x"
        assert cache_key("r", long) == cache_key("r", long)


class TestItDoesNotCollide:
    def test_two_projects_under_one_long_prefix_differ(self):
        """Why hashing rather than truncating.

        `[:64]` is right until two identities share a prefix longer than the
        limit — then they truncate to the same key and silently share a cache
        with another project's prompts in it. Deep paths under one parent is
        the normal case, not a contrived one.
        """
        base = "/Users/someone/projects/company/a_rather_long_parent_directory/"
        one = cache_key("code_gantry", base + "project-one/.code_gantry")
        two = cache_key("code_gantry", base + "project-two/.code_gantry")
        assert one != two

    def test_the_role_still_separates_two_roles_on_one_project(self):
        identity = "/a/long/identity/" + "z" * 80
        assert cache_key("exec", identity) != cache_key("code_gantry", identity)
