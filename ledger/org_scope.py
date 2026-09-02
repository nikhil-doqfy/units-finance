"""
get_pmc_ids_for_user_profile(user_profile_ref) -- Story 3.1's shared
PMC-scoping helper.

Replicates exactly two of units-backend's real `org_scope.get_pmc_ids_for_user()`
role branches (PropertyManager, Owner) -- both real, working logic, confirmed
by investigation against `utilities/org_scope.py:16-58` (spec Intent). The
Tenant branch is explicitly out of scope for this story: its upstream logic
filters `Lease.is_active`, a field that does not exist on the real `Lease`
model -- a pre-existing, already-broken upstream code path this story does
not reproduce or fix (spec Never, Design Notes).

Defined once here and reused by every future reporting view (Stories
3.2-3.5) -- never re-derived per-endpoint (spec Boundaries & Constraints,
AD-19). Finance never imports units-backend's `utilities.org_scope` -- this
re-implements the same logic against Finance's own unmanaged `*Ref` models
(AD-19, spec Never).

One deliberate deviation (human-confirmed, spec Boundaries & Constraints):
the PropertyManager branch's null-`company_id` fallback returns `[]` here,
whereas the real `get_pmc_ids_for_user()` returns `[pm.company_id]` (a
one-element list containing `None`) in that case -- Finance's version is
the corrected, sane behavior, not a bug to fix toward parity.
"""
from ledger.models import (
    OwnerRef,
    PMCPMMappingRef,
    PropertyBlocksRef,
    PropertyManagerRef,
    PropertyRef,
    UnitOwnerRef,
    UnitRef,
)


def get_pmc_ids_for_user_profile(user_profile_ref):
    """Return the list of pmc_ids the given `UserProfileRef` can reach.

    Mirrors `utilities.org_scope.get_pmc_ids_for_user()`'s PropertyManager
    and Owner branches exactly (see module docstring). Returns `[]` -- not
    an error, not an exception -- if neither branch matches (including any
    Tenant-only profile), matching units-backend's own `return []`
    fallthrough.
    """
    # PropertyManager branch: PropertyManager/Owner are UserProfile
    # subclasses in the real schema (Django multi-table inheritance), so
    # their table's PK IS the owning UserProfile.id -- look up by
    # pk=user_profile_ref.id, matching
    # `PropertyManager.objects.filter(pk=user_profile.pk)` exactly.
    pm = PropertyManagerRef.objects.filter(pk=user_profile_ref.id).first()
    if pm:
        mapped = list(
            PMCPMMappingRef.objects.filter(pm_id=pm.pk).values_list(
                "pmc_id", flat=True
            )
        )
        if mapped:
            return mapped
        # Fallback to the PropertyManager's own company_id -- may be null,
        # in which case this yields no PMCs, not an error (spec Boundaries
        # & Constraints).
        return [pm.company_id] if pm.company_id is not None else []

    # Owner branch: union of two join paths through
    # UnitOwnerRef/UnitRef/PropertyRef, deduplicated.
    owner = OwnerRef.objects.filter(pk=user_profile_ref.id).first()
    if owner:
        unit_ids = list(
            UnitOwnerRef.objects.filter(owner_id=owner.pk).values_list(
                "unit_id", flat=True
            )
        )
        if not unit_ids:
            return []

        units = list(
            UnitRef.objects.filter(pk__in=unit_ids).values_list(
                "parent_property_id", "property_block_tower_id"
            )
        )

        # (a) direct path: UnitRef.parent_property_id -> PropertyRef.pmc_id
        direct_property_ids = {p for p, _ in units if p is not None}
        via_direct = set(
            PropertyRef.objects.filter(pk__in=direct_property_ids).values_list(
                "pmc_id", flat=True
            )
        )

        # (b) block-tower path: UnitRef.property_block_tower_id ->
        # PropertyBlocksRef.property_id -> PropertyRef.pmc_id
        block_tower_ids = {bt for _, bt in units if bt is not None}
        block_property_ids = set(
            PropertyBlocksRef.objects.filter(pk__in=block_tower_ids).values_list(
                "property_id", flat=True
            )
        )
        via_blocks = set(
            PropertyRef.objects.filter(pk__in=block_property_ids).values_list(
                "pmc_id", flat=True
            )
        )

        pmc_ids = via_direct | via_blocks
        pmc_ids.discard(None)
        return list(pmc_ids)

    # Neither branch matches (including any Tenant-only profile) -- empty
    # scope, not an error (spec I/O matrix).
    return []
