// The response HTML includes player details and budgets, including after a re-auction.
let auctionRefreshPending = false;
window.refreshAuction = async function () {
  if (auctionRefreshPending) return;
  auctionRefreshPending = true;
  try {
    const response = await fetch(location.href, { cache: 'no-store' });
    if (!response.ok || response.redirected) throw new Error('Room unavailable');
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    if (!page.querySelector('#auction')) throw new Error('Auction unavailable');
    const position = { top: window.scrollY, left: window.scrollX };
    for (const id of ['auction', 'captain-summary', 'team-rosters']) {
      const current = document.getElementById(id);
      const replacement = page.getElementById(id);
      if (current && replacement) current.replaceWith(replacement);
    }
    requestAnimationFrame(() => window.scrollTo({ ...position, behavior: 'instant' }));
  } catch (error) {
    const message = document.querySelector('#auction .auction-refresh-error');
    if (message) {
      message.textContent = '刷新失败，请重试或刷新比赛房间状态。';
      message.hidden = false;
    }
  } finally {
    auctionRefreshPending = false;
  }
};
