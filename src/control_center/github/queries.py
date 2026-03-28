MY_PRS_QUERY = """
{
  viewer {
    pullRequests(first: 30, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        title
        url
        isDraft
        headRefName
        createdAt
        updatedAt
        repository {
          nameWithOwner
          isArchived
        }
        author {
          login
        }
        reviewDecision
        reviews(last: 20) {
          nodes {
            author {
              login
            }
            state
            body
          }
        }
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
                contexts(first: 50) {
                  nodes {
                    ... on CheckRun {
                      name
                      status
                      conclusion
                    }
                    ... on StatusContext {
                      context
                      state
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

REVIEW_REQUESTS_QUERY = """
query($searchQuery: String!) {
  search(query: $searchQuery, type: ISSUE, first: 30) {
    nodes {
      ... on PullRequest {
        number
        title
        url
        createdAt
        updatedAt
        repository {
          nameWithOwner
          isArchived
        }
        author {
          login
        }
        reviewDecision
        reviews(last: 20) {
          nodes {
            author {
              login
            }
            state
          }
        }
      }
    }
  }
}
"""
