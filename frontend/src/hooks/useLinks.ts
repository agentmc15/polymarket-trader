import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';
import type { ApproveLinkRequest, LinkStatus, RejectLinkRequest } from '../types';

/**
 * List event links for review (`GET /api/v1/links`), defaulting to the
 * `"proposed"` review queue — the human half of the event-equivalence
 * subsystem (PLAN.md D9, backend/app/api/routes/links.py). `POST
 * /links/propose` may run on a schedule, so this polls rather than
 * fetching once: the queue can grow between renders with no action
 * taken here.
 */
export function useLinks(status: LinkStatus | undefined = 'proposed') {
  return useQuery({
    queryKey: ['links', status],
    queryFn: () => api.getLinks(status),
    refetchInterval: 15000,
  });
}

/**
 * One link's full review payload (`GET /api/v1/links/{id}`) — both
 * venues' `rules_text` and the rest of the field-by-field comparison a
 * reviewer decides on.
 */
export function useLinkReview(linkId: number | null) {
  return useQuery({
    queryKey: ['links', 'review', linkId],
    queryFn: () => api.getLink(linkId!),
    enabled: linkId !== null,
  });
}

/**
 * Approve one link (`POST /links/{id}/approve`). Deliberately no bulk
 * variant — see links.py's module docstring for why approval is
 * one-at-a-time by design, not an oversight.
 */
export function useApproveLink() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ linkId, request }: { linkId: number; request: ApproveLinkRequest }) =>
      api.approveLink(linkId, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['links'] });
    },
  });
}

/** Reject one link (`POST /links/{id}/reject`). */
export function useRejectLink() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ linkId, request }: { linkId: number; request: RejectLinkRequest }) =>
      api.rejectLink(linkId, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['links'] });
    },
  });
}
