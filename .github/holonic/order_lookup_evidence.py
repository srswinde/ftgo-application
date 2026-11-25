"""Build and verify the source-backed FTGO GET /orders/{orderId} slice."""
import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

GATEWAY = 'ftgo-api-gateway/src/main/java/net/chrisrichardson/ftgo/apiagateway/'
ORDER = 'ftgo-order-service/src/main/java/net/chrisrichardson/ftgo/orderservice/'
JAVA = [
    ('FTGO gateway', GATEWAY + 'orders/OrderConfiguration.java',
     r'GET\("/orders/\{orderId\}"\),\s*orderHandlers::getOrderDetails'),
    ('FTGO gateway', GATEWAY + 'orders/OrderHandlers.java',
     r'String orderId = serverRequest\.pathVariable\("orderId"\);\s*Mono<OrderInfo> orderInfo = orderService\.findOrderById\(orderId\);'),
    ('FTGO gateway', GATEWAY + 'proxies/OrderServiceProxy.java',
     r'\.uri\(orderDestinations\.getOrderServiceUrl\(\) \+ "/orders/\{orderId\}", orderId\)'),
    ('Order service', ORDER + 'web/OrderController.java',
     r'@RequestMapping\(path = "/\{orderId\}", method = RequestMethod\.GET\)\s*public ResponseEntity<GetOrderResponse> getOrder\(@PathVariable long orderId\)'),
    ('Order service', ORDER + 'web/OrderController.java',
     r'orderRepository\.findById\(orderId\)'),
]


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True)


def source(repo, revision, path):
    return git(repo, 'show', f'{revision}:{path}')


def anchor(repo, revision, path, pattern, group, repository):
    text = source(repo, revision, path)
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError(f'Expected one source match for {group} at {revision[:12]}:{path}')
    line = text.count('\n', 0, matches[0].start()) + 1
    return dict(group=group, path=path, line=line, revision=revision,
                href=f'https://github.com/{repository}/blob/{revision}/{path}#L{line}')


def java_graph(repo, revision):
    revision = git(repo, 'rev-parse', '--verify', revision + '^{commit}').strip()
    controller = source(repo, revision, JAVA[-1][1])
    if not re.search(r'@RequestMapping\(path = "/orders"\)', controller):
        raise ValueError('Java controller has no /orders class mapping')
    handlers = source(repo, revision, JAVA[1][1])
    if not re.search(r'private OrderServiceProxy orderService;', handlers):
        raise ValueError('Gateway order handler has no resolved OrderServiceProxy')
    compose = source(repo, revision, 'docker-compose.yml')
    if not re.search(r'^[ \t]+ORDER_DESTINATIONS_ORDERSERVICEURL: http://ftgo-order-service:8080\s*$', compose, re.M):
        raise ValueError('Gateway order destination no longer resolves to the Java order service')
    proxy = source(repo, revision, JAVA[2][1])
    if not re.search(r'\.get\(\)\s*\.uri\(orderDestinations\.getOrderServiceUrl', proxy):
        raise ValueError('Java proxy does not issue GET to the configured order service')
    return [anchor(repo, revision, path, pattern, group, 'srswinde/ftgo-application')
            for group, path, pattern in JAVA]


def run_repository_probe(repo, revision, order_id):
    """Execute the pinned getOrder body with a recording repository and minimal DTOs."""
    text = source(repo, revision, JAVA[-1][1])
    method = re.search(r'public ResponseEntity<GetOrderResponse> getOrder\(@PathVariable long orderId\) \{\n(.*?)\n  \}', text, re.S)
    if not method:
        raise ValueError('Could not isolate the pinned getOrder method for the Java fixture')
    fixture = '''
import java.util.Optional;
class OrderLookupProbe {
    static long observed;
    static final Repository orderRepository = new Repository();
    static class Repository {
        Optional<Order> findById(long id) {
            observed = id;
            return Optional.of(new Order());
        }
    }
    static class Order {}
    static class GetOrderResponse {}
    enum HttpStatus { OK, NOT_FOUND }
    static class ResponseEntity<T> {
        ResponseEntity(HttpStatus status) {}
        ResponseEntity(T body, HttpStatus status) {}
    }
    static GetOrderResponse makeGetOrderResponse(Order order) { return new GetOrderResponse(); }
    public static void main(String[] args) {
        getOrder(Long.parseLong(args[0]));
        System.out.println(observed);
    }
    public static ResponseEntity<GetOrderResponse> getOrder(long orderId) {
%s
    }
}
''' % method[1]
    with tempfile.TemporaryDirectory(prefix='ftgo-java-probe-') as directory:
        path = Path(directory) / 'OrderLookupProbe.java'
        path.write_text(fixture, encoding='utf-8')
        result = subprocess.check_output(['java', str(path), order_id], text=True, timeout=30).strip()
    if not result.isdecimal():
        raise ValueError('Java lookup probe did not return a recorded order ID')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    graph = java_graph(args.repo, args.revision)
    observed = run_repository_probe(args.repo, graph[0]['revision'], '123')
    if observed != '123':
        raise ValueError(f'Expected Java repository lookup for 123, got {observed}')
    args.output.write_text(json.dumps(dict(route='GET /orders/{orderId}', trace=graph,
                                          fixture_repository_id=observed), indent=2) + '\n')


if __name__ == '__main__':
    main()
