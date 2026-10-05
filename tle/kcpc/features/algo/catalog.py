"""The topics of the algorithm of the month, and where to read about each.

The usual competitive-programming syllabus, from prefix sums to maximum flow:
each topic links its GeeksforGeeks article and, if it has one, its
cp-algorithms article, at the addresses both sites gave them in October 2026
(after any redirect). A live test checks that they still answer.

A topic's ``slug`` is what the servers' picks store, so it never changes. A
topic taken out of the catalog stays in the servers' history, and the picks
leave it out (see ``service``).
"""

from dataclasses import dataclass
from enum import Enum


class Level(str, Enum):
    """How far into the syllabus a topic is."""

    BEGINNER = 'beginner'
    INTERMEDIATE = 'intermediate'
    ADVANCED = 'advanced'


@dataclass(frozen=True)
class AlgoTopic:
    """A data structure or algorithm that a server can have as its topic."""

    slug: str  # its stable ID, as stored: lowercase letters, digits and hyphens
    name: str
    summary: str  # what it is and what it's for, in a sentence or two
    level: Level
    gfg_url: str  # its GeeksforGeeks article
    cp_algorithms_url: str | None  # its cp-algorithms article, if it has one


_GFG = 'https://www.geeksforgeeks.org/dsa'
_CP_ALGORITHMS = 'https://cp-algorithms.com'

ALGO_TOPICS: tuple[AlgoTopic, ...] = (
    AlgoTopic(
        'prefix-sums',
        'Prefix sums',
        'Store the running totals of an array, so that the sum of any range is '
        'the difference of two of them: O(1) a query after O(n) preparation. The '
        'same works for rectangles in a grid.',
        Level.BEGINNER,
        f'{_GFG}/prefix-sum-array-implementation-applications-competitive-programming/',
        None,
    ),
    AlgoTopic(
        'two-pointers',
        'Two pointers',
        'Move two indices through a sorted array, each only forwards, to find '
        'pairs or windows with some property in O(n) rather than O(n^2).',
        Level.BEGINNER,
        f'{_GFG}/two-pointers-technique/',
        None,
    ),
    AlgoTopic(
        'binary-search',
        'Binary search',
        'Halve a sorted range at each step to find a value in O(log n), or '
        'search on the answer: the smallest value that passes a check which, '
        'once true, stays true.',
        Level.BEGINNER,
        f'{_GFG}/binary-search/',
        f'{_CP_ALGORITHMS}/num_methods/binary_search.html',
    ),
    AlgoTopic(
        'greedy',
        'Greedy algorithms',
        'Build an answer by always taking the choice that looks best now, often '
        'after sorting by the right key. An exchange argument shows when that is '
        'optimal.',
        Level.BEGINNER,
        f'{_GFG}/greedy-algorithms/',
        None,
    ),
    AlgoTopic(
        'monotonic-stack',
        'Monotonic stack',
        'A stack kept in increasing or decreasing order, which finds the next '
        'greater or smaller element of every position, or the largest rectangle '
        'in a histogram, in O(n) in all.',
        Level.BEGINNER,
        f'{_GFG}/introduction-to-monotonic-stack-2/',
        None,
    ),
    AlgoTopic(
        'sliding-window-deque',
        'Sliding window minimum',
        'A deque kept in sorted order gives the minimum or maximum of every '
        'window of a fixed length in O(n) in all, and speeds up some DP '
        'transitions.',
        Level.BEGINNER,
        f'{_GFG}/sliding-window-maximum-maximum-of-all-subarrays-of-size-k/',
        f'{_CP_ALGORITHMS}/data_structures/stack_queue_modification.html',
    ),
    AlgoTopic(
        'hash-tables',
        'Hash tables',
        'Sets and maps that hash their keys for lookups in O(1) on average, to '
        'count, remove duplicates or find pairs; and how inputs made to collide '
        'slow them down.',
        Level.BEGINNER,
        f'{_GFG}/hashing-data-structure/',
        None,
    ),
    AlgoTopic(
        'breadth-first-search',
        'Breadth-first search',
        'Explore a graph level by level with a queue, which finds the shortest '
        'paths in a graph or grid whose edges all have the same length.',
        Level.BEGINNER,
        f'{_GFG}/breadth-first-search-or-bfs-for-a-graph/',
        f'{_CP_ALGORITHMS}/graph/breadth-first-search.html',
    ),
    AlgoTopic(
        'depth-first-search',
        'Depth-first search',
        'Explore a graph as deep as possible before backtracking, recursively or '
        'with a stack: the basis of connected components, cycle detection and '
        'most algorithms on trees.',
        Level.BEGINNER,
        f'{_GFG}/depth-first-search-or-dfs-for-a-graph/',
        f'{_CP_ALGORITHMS}/graph/depth-first-search.html',
    ),
    AlgoTopic(
        'sieve-of-eratosthenes',
        'Sieve of Eratosthenes',
        'Find every prime up to n in O(n log log n) by crossing out multiples. '
        "Keeping each number's smallest prime factor makes factorizing fast.",
        Level.BEGINNER,
        f'{_GFG}/sieve-of-eratosthenes/',
        f'{_CP_ALGORITHMS}/algebra/sieve-of-eratosthenes.html',
    ),
    AlgoTopic(
        'binary-exponentiation',
        'Binary exponentiation',
        'Compute a^n, usually modulo m, with O(log n) multiplications, by '
        'squaring and following the binary digits of n.',
        Level.BEGINNER,
        f'{_GFG}/modular-exponentiation-power-in-modular-arithmetic/',
        f'{_CP_ALGORITHMS}/algebra/binary-exp.html',
    ),
    AlgoTopic(
        'modular-inverse',
        'Modular arithmetic and inverses',
        'Keep huge answers small by working modulo a prime, and divide by '
        "multiplying with a modular inverse, from Fermat's little theorem or the "
        'extended Euclidean algorithm.',
        Level.BEGINNER,
        f'{_GFG}/multiplicative-inverse-under-modulo-m/',
        f'{_CP_ALGORITHMS}/algebra/module-inverse.html',
    ),
    AlgoTopic(
        'topological-sort',
        'Topological sort',
        'Order the vertices of a directed acyclic graph so that every edge points '
        "forwards, with DFS or Kahn's algorithm: the order for DP over a DAG.",
        Level.INTERMEDIATE,
        f'{_GFG}/topological-sorting/',
        f'{_CP_ALGORITHMS}/graph/topological-sort.html',
    ),
    AlgoTopic(
        'dijkstra',
        "Dijkstra's algorithm",
        'Shortest paths from one vertex when no edge is negative, settling the '
        'vertices nearest first with a priority queue, in O((n + m) log n).',
        Level.INTERMEDIATE,
        f'{_GFG}/dijkstras-shortest-path-algorithm-greedy-algo-7/',
        f'{_CP_ALGORITHMS}/graph/dijkstra.html',
    ),
    AlgoTopic(
        'bellman-ford',
        'Bellman-Ford algorithm',
        'Shortest paths from one vertex even with negative edges, relaxing every '
        'edge n - 1 times in O(nm); a change after that reveals a negative '
        'cycle.',
        Level.INTERMEDIATE,
        f'{_GFG}/bellman-ford-algorithm-dp-23/',
        f'{_CP_ALGORITHMS}/graph/bellman_ford.html',
    ),
    AlgoTopic(
        'floyd-warshall',
        'Floyd-Warshall algorithm',
        'Shortest paths between every pair of vertices in O(n^3) with three '
        'nested loops over a matrix of distances: fine for a few hundred '
        'vertices.',
        Level.INTERMEDIATE,
        f'{_GFG}/floyd-warshall-algorithm-dp-16/',
        f'{_CP_ALGORITHMS}/graph/all-pair-shortest-path-floyd-warshall.html',
    ),
    AlgoTopic(
        'minimum-spanning-tree',
        'Minimum spanning tree',
        "Connect every vertex for the least total weight: Kruskal's algorithm "
        "adds the lightest edges that join two components, and Prim's grows one "
        'tree.',
        Level.INTERMEDIATE,
        f'{_GFG}/kruskals-minimum-spanning-tree-algorithm-greedy-algo-2/',
        f'{_CP_ALGORITHMS}/graph/mst_kruskal.html',
    ),
    AlgoTopic(
        'disjoint-set-union',
        'Disjoint set union',
        'Keep elements in disjoint sets, merging two sets and finding the set of '
        'an element in nearly O(1) each, with path compression and union by '
        'size.',
        Level.INTERMEDIATE,
        f'{_GFG}/introduction-to-disjoint-set-data-structure-or-union-find-algorithm/',
        f'{_CP_ALGORITHMS}/data_structures/disjoint_set_union.html',
    ),
    AlgoTopic(
        'lowest-common-ancestor',
        'Lowest common ancestor',
        "Store each vertex's ancestors 1, 2, 4, ... levels up (binary lifting) "
        'to find the lowest common ancestor of two vertices of a tree, and so '
        'their distance, in O(log n).',
        Level.INTERMEDIATE,
        f'{_GFG}/lca-in-a-tree-using-binary-lifting-technique/',
        f'{_CP_ALGORITHMS}/graph/lca_binary_lifting.html',
    ),
    AlgoTopic(
        'segment-tree',
        'Segment tree',
        "A binary tree over an array's ranges that answers range queries, such "
        'as sums or minimums, and takes point updates, in O(log n) each.',
        Level.INTERMEDIATE,
        f'{_GFG}/segment-tree-data-structure/',
        f'{_CP_ALGORITHMS}/data_structures/segment_tree.html',
    ),
    AlgoTopic(
        'fenwick-tree',
        'Fenwick tree',
        'An array that keeps prefix sums up to date under point updates in '
        'O(log n) each: shorter to write than a segment tree when sums are all '
        'you need.',
        Level.INTERMEDIATE,
        f'{_GFG}/binary-indexed-tree-or-fenwick-tree-2/',
        f'{_CP_ALGORITHMS}/data_structures/fenwick.html',
    ),
    AlgoTopic(
        'sparse-table',
        'Sparse table',
        'Answers for every range whose length is a power of two, computed in '
        "O(n log n), give range minimums in O(1) on an array that doesn't "
        'change.',
        Level.INTERMEDIATE,
        f'{_GFG}/sparse-table/',
        f'{_CP_ALGORITHMS}/data_structures/sparse-table.html',
    ),
    AlgoTopic(
        'trie',
        'Trie',
        'A tree of the prefixes of a set of strings, a character on each edge, '
        'for fast prefix lookups; a trie of bits finds the largest XOR of two '
        'numbers.',
        Level.INTERMEDIATE,
        f'{_GFG}/trie-insert-and-search/',
        None,
    ),
    AlgoTopic(
        'prefix-function',
        'KMP and the prefix function',
        'For each prefix of a string, the length of its longest proper prefix '
        'that is also its suffix: it finds every occurrence of a pattern in '
        'O(n + m) (Knuth-Morris-Pratt).',
        Level.INTERMEDIATE,
        f'{_GFG}/kmp-algorithm-for-pattern-searching/',
        f'{_CP_ALGORITHMS}/string/prefix-function.html',
    ),
    AlgoTopic(
        'z-function',
        'Z-function',
        'For each position of a string, the length of the longest substring '
        'starting there that is also a prefix of it, in O(n): pattern matching '
        'and periods another way.',
        Level.INTERMEDIATE,
        f'{_GFG}/z-algorithm-linear-time-pattern-searching-algorithm/',
        f'{_CP_ALGORITHMS}/string/z-function.html',
    ),
    AlgoTopic(
        'string-hashing',
        'String hashing',
        'Turn every substring into a number with a polynomial rolling hash, to '
        'compare substrings in O(1) after O(n) preparation, at a small risk of '
        'collisions.',
        Level.INTERMEDIATE,
        f'{_GFG}/string-hashing-using-polynomial-rolling-hash-function/',
        f'{_CP_ALGORITHMS}/string/string-hashing.html',
    ),
    AlgoTopic(
        'knapsack',
        'Knapsack DP',
        'Choose items of given weights and values for the most value within a '
        'capacity, with a DP over the items and the capacity in O(nW): the model '
        'of many subset-sum problems.',
        Level.INTERMEDIATE,
        f'{_GFG}/0-1-knapsack-problem-dp-10/',
        f'{_CP_ALGORITHMS}/dynamic_programming/knapsack.html',
    ),
    AlgoTopic(
        'longest-increasing-subsequence',
        'Longest increasing subsequence',
        'The longest subsequence whose values increase, in O(n log n), by '
        'keeping the smallest possible last value of each length and binary '
        'searching it.',
        Level.INTERMEDIATE,
        f'{_GFG}/longest-increasing-subsequence-dp-3/',
        f'{_CP_ALGORITHMS}/sequences/longest_increasing_subsequence.html',
    ),
    AlgoTopic(
        'bitmask-dp',
        'Bitmask DP',
        'DP over subsets written as the bits of an integer, for up to about 20 '
        'items, such as the travelling salesman problem in O(2^n n^2).',
        Level.INTERMEDIATE,
        f'{_GFG}/travelling-salesman-problem-using-dynamic-programming/',
        f'{_CP_ALGORITHMS}/algebra/all-submasks.html',
    ),
    AlgoTopic(
        'tree-dp',
        'DP on trees',
        "Compute an answer for every subtree from its children's in one DFS, as "
        'for the largest independent set; rerooting then gives the answer for '
        'every root.',
        Level.INTERMEDIATE,
        f'{_GFG}/introduction-to-dynamic-programming-on-trees/',
        None,
    ),
    AlgoTopic(
        'binomial-coefficients',
        'Binomial coefficients',
        'Count the ways to choose k of n items modulo a prime, with factorials '
        "and their inverses computed once, or with Pascal's triangle for small "
        'n.',
        Level.INTERMEDIATE,
        f'{_GFG}/binomial-coefficient-dp-9/',
        f'{_CP_ALGORITHMS}/combinatorics/binomial-coefficients.html',
    ),
    AlgoTopic(
        'extended-euclid',
        'Extended Euclidean algorithm',
        'Find gcd(a, b) and integers x and y with ax + by = gcd(a, b), which '
        'solves linear Diophantine equations and gives modular inverses.',
        Level.INTERMEDIATE,
        f'{_GFG}/euclidean-algorithms-basic-and-extended/',
        f'{_CP_ALGORITHMS}/algebra/extended-euclid-algorithm.html',
    ),
    AlgoTopic(
        'matrix-exponentiation',
        'Matrix exponentiation',
        'Raise a k by k matrix to the n-th power in O(k^3 log n), to get the '
        'n-th term of a linear recurrence such as the Fibonacci numbers, or to '
        'count walks of length n.',
        Level.INTERMEDIATE,
        f'{_GFG}/matrix-exponentiation/',
        None,
    ),
    AlgoTopic(
        'bridges',
        'Bridges and articulation points',
        'Find the edges and vertices whose removal disconnects a graph, in one '
        'DFS, by comparing when each vertex was reached with the earliest vertex '
        'its subtree reaches back to.',
        Level.INTERMEDIATE,
        f'{_GFG}/bridge-in-a-graph/',
        f'{_CP_ALGORITHMS}/graph/bridge-searching.html',
    ),
    AlgoTopic(
        'strongly-connected-components',
        'Strongly connected components',
        'Split a directed graph into the largest groups of vertices that all '
        'reach each other (Kosaraju or Tarjan); shrinking each to a vertex '
        'leaves a DAG. 2-SAT is built on it.',
        Level.ADVANCED,
        f'{_GFG}/strongly-connected-components/',
        f'{_CP_ALGORITHMS}/graph/strongly-connected-components.html',
    ),
    AlgoTopic(
        'lazy-propagation',
        'Lazy propagation',
        'Lets a segment tree update a whole range in O(log n), by leaving '
        'pending updates at its nodes and passing them down only when needed.',
        Level.ADVANCED,
        f'{_GFG}/lazy-propagation-in-segment-tree/',
        f'{_CP_ALGORITHMS}/data_structures/segment_tree.html'
        '#range-updates-lazy-propagation',
    ),
    AlgoTopic(
        'sqrt-decomposition',
        'Sqrt decomposition',
        'Split an array into blocks of about sqrt(n) elements to answer range '
        "queries and updates in O(sqrt(n)); Mo's algorithm orders offline "
        'queries in the same spirit.',
        Level.ADVANCED,
        f'{_GFG}/square-root-sqrt-decomposition-algorithm/',
        f'{_CP_ALGORITHMS}/data_structures/sqrt_decomposition.html',
    ),
    AlgoTopic(
        'suffix-array',
        'Suffix array',
        "The sorted order of a string's suffixes, built in O(n log n); with the "
        'LCP array it counts distinct substrings and answers many substring '
        'queries.',
        Level.ADVANCED,
        f'{_GFG}/suffix-array-set-1-introduction/',
        f'{_CP_ALGORITHMS}/string/suffix-array.html',
    ),
    AlgoTopic(
        'digit-dp',
        'Digit DP',
        'Count the numbers up to N whose digits have some property, building '
        "them digit by digit while remembering whether they still follow N's "
        'digits.',
        Level.ADVANCED,
        f'{_GFG}/digit-dp-introduction/',
        None,
    ),
    AlgoTopic(
        'convex-hull',
        'Convex hull',
        'The smallest convex polygon around a set of points, in O(n log n), by '
        'sorting them and keeping only left turns (Graham scan or the monotone '
        'chain).',
        Level.ADVANCED,
        f'{_GFG}/convex-hull-using-graham-scan/',
        f'{_CP_ALGORITHMS}/geometry/convex-hull.html',
    ),
    AlgoTopic(
        'maximum-flow',
        'Maximum flow',
        'Send as much as possible from a source to a sink through edges of '
        'limited capacity, along augmenting paths (Edmonds-Karp or Dinic); the '
        'maximum flow equals the minimum cut.',
        Level.ADVANCED,
        f'{_GFG}/ford-fulkerson-algorithm-for-maximum-flow-problem/',
        f'{_CP_ALGORITHMS}/graph/edmonds_karp.html',
    ),
    AlgoTopic(
        'bipartite-matching',
        'Bipartite matching',
        'Pair up as many vertices of the two sides of a bipartite graph as '
        "possible, with Kuhn's augmenting paths in O(nm), or as a maximum flow.",
        Level.ADVANCED,
        f'{_GFG}/maximum-bipartite-matching/',
        f'{_CP_ALGORITHMS}/graph/kuhn_maximum_bipartite_matching.html',
    ),
    AlgoTopic(
        'sprague-grundy',
        'Sprague-Grundy theorem',
        'Every position of an impartial game acts like a Nim heap of its Grundy '
        "number, the mex of its moves' numbers, so a sum of games is a win when "
        'their XOR is not zero.',
        Level.ADVANCED,
        f'{_GFG}/combinatorial-game-theory-set-4-sprague-grundy-theorem/',
        f'{_CP_ALGORITHMS}/game_theory/sprague-grundy-nim.html',
    ),
)

_BY_SLUG = {found.slug: found for found in ALGO_TOPICS}


def topic(slug: str) -> AlgoTopic | None:
    """The catalog's topic with ``slug``; None if it has none, or no longer."""
    return _BY_SLUG.get(slug)
