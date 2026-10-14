function paginateItems(items, page, pageSize) {
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const current = Math.min(Math.max(1, page), pages);
  const start = (current - 1) * pageSize;
  return { items: items.slice(start, start + pageSize), page: current, pages, start };
}

if (typeof module !== "undefined") {
  module.exports = paginateItems;
  if (require.main === module) {
    const result = paginateItems([1, 2, 3, 4, 5], 3, 2);
    if (JSON.stringify(result) !== JSON.stringify({ items: [5], page: 3, pages: 3, start: 4 })) throw new Error("pagination self-check failed");
  }
}
