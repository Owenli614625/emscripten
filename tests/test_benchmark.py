import math, os, shutil, subprocess
import runner
from runner import RunnerCore, path_from_root
from tools.shared import *

# standard arguments for timing:
# 0: no runtime, just startup
# 1: very little runtime
# 2: 0.5 seconds
# 3: 1 second
# 4: 5 seconds
# 5: 10 seconds
DEFAULT_ARG = '4'

TEST_REPS = 2

CORE_BENCHMARKS = True # core benchmarks vs full regression suite

class Benchmarker:
  def __init__(self, name):
    self.name = name

  def bench(self, args, output_parser=None):
    self.times = []
    for i in range(TEST_REPS):
      start = time.time()
      output = self.run(args)
      if not output_parser:
        curr = time.time()-start
      else:
        curr = output_parser(output)
      self.times.append(curr)

  def display(self, baseline=None):
    if baseline == self: baseline = None
    mean = sum(self.times)/len(self.times)
    squared_times = map(lambda x: x*x, self.times)
    mean_of_squared = sum(squared_times)/len(self.times)
    std = math.sqrt(mean_of_squared - mean*mean)
    sorted_times = self.times[:]
    sorted_times.sort()
    median = sum(sorted_times[len(sorted_times)/2 - 1:len(sorted_times)/2 + 1])/2

    print '   %10s: mean: %4.3f (+-%4.3f) secs  median: %4.3f  range: %4.3f-%4.3f  (noise: %4.3f%%)  (%d runs)' % (self.name, mean, std, median, min(self.times), max(self.times), 100*std/mean, TEST_REPS),

    if baseline:
      mean_baseline = sum(baseline.times)/len(baseline.times)
      final = mean / mean_baseline
      print '  Relative: %.2f X slower' % final
    else:
      print

class NativeBenchmarker(Benchmarker):
  def __init__(self, name, cc, cxx):
    self.name = name
    self.cc = cc
    self.cxx = cxx

  def build(self, parent, filename, args, shared_args, emcc_args, native_args, native_exec, lib_builder):
    self.parent = parent
    if lib_builder: native_args += lib_builder(self.name, native=True, env_init={ 'CC': self.cc, 'CXX': self.cxx })
    if not native_exec:
      compiler = self.cxx if filename.endswith('cpp') else self.cc
      process = Popen([compiler, '-O2', '-fno-math-errno', filename, '-o', filename+'.native'] + shared_args + native_args, stdout=PIPE, stderr=parent.stderr_redirect)
      output = process.communicate()
      if process.returncode is not 0:
        print >> sys.stderr, "Building native executable with command '%s' failed with a return code %d!" % (' '.join([compiler, '-O2', filename, '-o', filename+'.native']), process.returncode)
        print "Output: " + output[0]
    else:
      print '(using clang)'
      shutil.copyfile(native_exec, filename + '.native')
      shutil.copymode(native_exec, filename + '.native')
    self.filename = filename

  def run(self, args):
    process = Popen([self.filename+'.native'] + args, stdout=PIPE, stderr=PIPE)
    return process.communicate()[0]

class JSBenchmarker(Benchmarker):
  def __init__(self, name, engine, extra_args=[]):
    self.name = name
    self.engine = engine
    self.extra_args = extra_args

  def build(self, parent, filename, args, shared_args, emcc_args, native_args, native_exec, lib_builder):
    self.filename = filename
    if lib_builder: emcc_args += lib_builder('js', native=False, env_init={})

    open('hardcode.py', 'w').write('''
def process(filename):
  js = open(filename).read()
  replaced = js.replace("run();", "run(%s.concat(Module[\\"arguments\\"]));")
  assert js != replaced
  open(filename, 'w').write(replaced)
import sys
process(sys.argv[1])
''' % str(args[:-1]) # do not hardcode in the last argument, the default arg
)

    try_delete(filename + '.js')
    output = Popen([PYTHON, EMCC, filename, #'-O3',
                    '-O2', '-s', 'DOUBLE_MODE=0', '-s', 'PRECISE_I64_MATH=0',
                    '--memory-init-file', '0', '--js-transform', 'python hardcode.py',
                    '-s', 'TOTAL_MEMORY=128*1024*1024',
                    #'--closure', '1',
                    #'-g',
                    '-o', filename + '.js'] + shared_args + emcc_args + self.extra_args, stdout=PIPE, stderr=PIPE).communicate()
    assert os.path.exists(filename + '.js'), 'Failed to compile file: ' + output[0]

  def run(self, args):
    return run_js(self.filename + '.js', engine=self.engine, args=args, stderr=PIPE, full_output=True)

# Benchmarkers
benchmarkers = [
  NativeBenchmarker('clang', CLANG_CC, CLANG),
  NativeBenchmarker('gcc', 'gcc', 'g++'),
  JSBenchmarker('sm-f32',       SPIDERMONKEY_ENGINE, ['-s', 'PRECISE_F32=2']),
  JSBenchmarker('sm',           SPIDERMONKEY_ENGINE),
  #JSBenchmarker('sm-noasm',     SPIDERMONKEY_ENGINE + ['--no-asmjs']),
  #JSBenchmarker('sm-noasm-f32', SPIDERMONKEY_ENGINE + ['--no-asmjs'], ['-s', 'PRECISE_F32=2']),
  JSBenchmarker('v8',           V8_ENGINE)
]

class benchmark(RunnerCore):
  save_dir = True

  @classmethod
  def setUpClass(self):
    super(benchmark, self).setUpClass()

    fingerprint = [time.asctime()]
    try:
      fingerprint.append('em: ' + Popen(['git', 'show'], stdout=PIPE).communicate()[0].split('\n')[0])
    except:
      pass
    try:
      d = os.getcwd()
      os.chdir(os.path.expanduser('~/Dev/mozilla-central'))
      fingerprint.append('sm: ' + filter(lambda line: 'changeset' in line,
                                         Popen(['hg', 'tip'], stdout=PIPE).communicate()[0].split('\n'))[0])
    except:
      pass
    finally:
      os.chdir(d)
    fingerprint.append('llvm: ' + LLVM_ROOT)
    print 'Running Emscripten benchmarks... [ %s ]' % ' | '.join(fingerprint)

    assert(os.path.exists(CLOSURE_COMPILER))

    try:
      index = SPIDERMONKEY_ENGINE.index("options('strict')")
      SPIDERMONKEY_ENGINE = SPIDERMONKEY_ENGINE[:index-1] + SPIDERMONKEY_ENGINE[index+1:] # closure generates non-strict
    except:
      pass

    Building.COMPILER = CLANG
    Building.COMPILER_TEST_OPTS = []

  def do_benchmark(self, name, src, expected_output='FAIL', args=[], emcc_args=[], native_args=[], shared_args=[], force_c=False, reps=TEST_REPS, native_exec=None, output_parser=None, args_processor=None, lib_builder=None):
    args = args or [DEFAULT_ARG]
    if args_processor: args = args_processor(args)

    dirname = self.get_dir()
    filename = os.path.join(dirname, name + '.c' + ('' if force_c else 'pp'))
    f = open(filename, 'w')
    f.write(src)
    f.close()

    print
    for b in benchmarkers:
      b.build(self, filename, args, shared_args, emcc_args, native_args, native_exec, lib_builder)
      b.bench(args, output_parser)
      b.display(benchmarkers[0])

  def test_primes(self):
    src = r'''
      #include<stdio.h>
      #include<math.h>
      int main(int argc, char **argv) {
        int arg = argc > 1 ? argv[1][0] - '0' : 3;
        switch(arg) {
          case 0: return 0; break;
          case 1: arg = 33000; break;
          case 2: arg = 130000; break;
          case 3: arg = 220000; break;
          case 4: arg = 610000; break;
          case 5: arg = 1010000; break;
          default: printf("error: %d\\n", arg); return -1;
        }

        int primes = 0, curri = 2;
        while (primes < arg) {
          int ok = true;
          for (int j = 2; j < sqrtf(curri); j++) {
            if (curri % j == 0) {
              ok = false;
              break;
            }
          }
          if (ok) {
            primes++;
          }
          curri++;
        }
        printf("lastprime: %d.\n", curri-1);
        return 0;
      }
    '''
    self.do_benchmark('primes', src, 'lastprime:')

  def test_memops(self):
    src = '''
      #include<stdio.h>
      #include<string.h>
      #include<stdlib.h>
      int main(int argc, char **argv) {
        int N, M;
        int arg = argc > 1 ? argv[1][0] - '0' : 3;
        switch(arg) {
          case 0: return 0; break;
          case 1: N = 1024*1024; M = 55; break;
          case 2: N = 1024*1024; M = 400; break;
          case 3: N = 1024*1024; M = 800; break;
          case 4: N = 1024*1024; M = 4000; break;
          case 5: N = 1024*1024; M = 8000; break;
          default: printf("error: %d\\n", arg); return -1;
        }

        int final = 0;
        char *buf = (char*)malloc(N);
        for (int t = 0; t < M; t++) {
          for (int i = 0; i < N; i++)
            buf[i] = (i + final)%256;
          for (int i = 0; i < N; i++)
            final += buf[i] & 1;
          final = final % 1000;
        }
        printf("final: %d.\\n", final);
        return 0;
      }
    '''
    self.do_benchmark('memops', src, 'final:')

  def zzztest_files(self):
    src = r'''
      #include<stdio.h>
      #include<stdlib.h>
      #include<assert.h>
      #include <unistd.h>

      int main() {
        int N = 100;
        int M = 1000;
        int K = 1000;
        unsigned char *k = (unsigned char*)malloc(K+1), *k2 = (unsigned char*)malloc(K+1);
        for (int i = 0; i < K; i++) {
          k[i] = (i % 250) + 1;
        }
        k[K] = 0;
        char buf[100];
        for (int i = 0; i < N; i++) {
          sprintf(buf, "/dev/shm/file-%d.dat", i);
          FILE *f = fopen(buf, "w");
          for (int j = 0; j < M; j++) {
            fwrite(k, 1, (j % K) + 1, f);
          }
          fclose(f);
        }
        for (int i = 0; i < N; i++) {
          sprintf(buf, "/dev/shm/file-%d.dat", i);
          FILE *f = fopen(buf, "r");
          for (int j = 0; j < M; j++) {
            fread(k2, 1, (j % K) + 1, f);
          }
          fclose(f);
          for (int j = 0; j < K; j++) {
            assert(k[j] == k2[j]);
          }
          unlink(buf);
        }
        printf("ok");
        return 0;
      }
    '''
    self.do_benchmark(src, 'ok')

  def test_copy(self):
    src = r'''
      #include<stdio.h>
      struct vec {
        int x, y, z;
        int r, g, b;
        vec(int x_, int y_, int z_, int r_, int g_, int b_) : x(x_), y(y_), z(z_), r(r_), g(g_), b(b_) {}
        static vec add(vec a, vec b) {
          return vec(a.x+b.x, a.y+b.y, a.z+b.z, a.r+b.r, a.g+b.g, a.b+b.b);
        }
        void norm() {
          x %= 1024;
          y %= 1024;
          z %= 1024;
          r %= 1024;
          b %= 1024;
          g %= 1024;
        }
        int sum() { return x + y + z + r + g + b; }
      };
      int main(int argc, char **argv) {
        int arg = argc > 1 ? argv[1][0] - '0' : 3;
        switch(arg) {
          case 0: return 0; break;
          case 1: arg = 75; break;
          case 2: arg = 625; break;
          case 3: arg = 1250; break;
          case 4: arg = 5*1250; break;
          case 5: arg = 10*1250; break;
          default: printf("error: %d\\n", arg); return -1;
        }

        int total = 0;
        for (int i = 0; i < arg; i++) {
          for (int j = 0; j < 50000; j++) {
            vec c(i, i+i%10, j*2, i%255, j%120, i%15);
            vec d(j+i%10, j*2, j%255, i%120, j%15, j);
            vec e = c;
            c.norm();
            d.norm();
            vec f = vec::add(c, d);
            f = vec::add(e, f);
            f.norm();
            f = vec::add(d, f);
            total += f.sum() % 100;
            total %= 10240;
          }
        }
        printf("sum:%d\n", total);
        return 0;
      }
    '''
    self.do_benchmark('copy', src, 'sum:')

  def test_fannkuch(self):
    src = open(path_from_root('tests', 'fannkuch.cpp'), 'r').read().replace(
      'int n = argc > 1 ? atoi(argv[1]) : 0;',
      '''
        int n;
        int arg = argc > 1 ? argv[1][0] - '0' : 3;
        switch(arg) {
          case 0: return 0; break;
          case 1: n = 9; break;
          case 2: n = 10; break;
          case 3: n = 11; break;
          case 4: n = 11; break;
          case 5: n = 12; break;
          default: printf("error: %d\\n", arg); return -1;
        }
      '''
    )
    assert 'switch(arg)' in src
    self.do_benchmark('fannkuch', src, 'Pfannkuchen(')

  def test_corrections(self):
    src = r'''
      #include<stdio.h>
      #include<math.h>
      int main(int argc, char **argv) {
        int N, M;
        int arg = argc > 1 ? argv[1][0] - '0' : 3;
        switch(arg) {
          case 0: return 0; break;
          case 1: N = 20000; M = 550; break;
          case 2: N = 20000; M = 3500; break;
          case 3: N = 20000; M = 7000; break;
          case 4: N = 20000; M = 5*7000; break;
          case 5: N = 20000; M = 10*7000; break;
          default: printf("error: %d\\n", arg); return -1;
        }

        unsigned int f = 0;
        unsigned short s = 0;
        for (int t = 0; t < M; t++) {
          for (int i = 0; i < N; i++) {
            f += i / ((t % 5)+1);
            if (f > 1000) f /= (t % 3)+1;
            if (i % 4 == 0) f += i * (i % 8 == 0 ? 1 : -1);
            s += (short(f)*short(f)) % 256;
          }
        }
        printf("final: %d:%d.\n", f, s);
        return 0;
      }
    '''
    self.do_benchmark('corrections', src, 'final:', emcc_args=['-s', 'CORRECT_SIGNS=1', '-s', 'CORRECT_OVERFLOWS=1', '-s', 'CORRECT_ROUNDINGS=1'])

  def fasta(self, name, double_rep, emcc_args=[]):
    src = open(path_from_root('tests', 'fasta.cpp'), 'r').read().replace('double', double_rep)
    src = src.replace('   const size_t n = ( argc > 1 ) ? atoi( argv[1] ) : 512;', '''
      int n;
      int arg = argc > 1 ? argv[1][0] - '0' : 3;
      switch(arg) {
        case 0: return 0; break;
        case 1: n = 19000000/20; break;
        case 2: n = 19000000/2; break;
        case 3: n = 19000000; break;
        case 4: n = 19000000*5; break;
        case 5: n = 19000000*10; break;
        default: printf("error: %d\\n", arg); return -1;
      }
    ''')
    assert 'switch(arg)' in src
    self.do_benchmark('fasta', src, '')

  def test_fasta_float(self):
    self.fasta('fasta_float', 'float')

  def test_fasta_double(self):
    if CORE_BENCHMARKS: return
    self.fasta('fasta_double', 'double')

  def test_fasta_double_full(self):
    if CORE_BENCHMARKS: return
    self.fasta('fasta_double_full', 'double', emcc_args=['-s', 'DOUBLE_MODE=1'])

  def test_skinning(self):
    src = open(path_from_root('tests', 'skinning_test_no_simd.cpp'), 'r').read()
    self.do_benchmark('skinning', src, 'blah=0.000000')

  def test_life(self):
    if CORE_BENCHMARKS: return
    src = open(path_from_root('tests', 'life.c'), 'r').read()
    self.do_benchmark('life', src, '''--------------------------------''', shared_args=['-std=c99'], force_c=True)

  def test_linpack_double(self):
    if CORE_BENCHMARKS: return
    def output_parser(output):
      return 100.0/float(re.search('Unrolled Double  Precision +([\d\.]+) Mflops', output).group(1))
    self.do_benchmark('linpack_double', open(path_from_root('tests', 'linpack.c')).read(), '''Unrolled Double  Precision''', force_c=True, output_parser=output_parser)

  def test_linpack_float(self): # TODO: investigate if this might benefit from -ffast-math in LLVM 3.3+ which has fast math stuff in LLVM IR
    def output_parser(output):
      return 100.0/float(re.search('Unrolled Single  Precision +([\d\.]+) Mflops', output).group(1))
    self.do_benchmark('linpack_float', open(path_from_root('tests', 'linpack.c')).read(), '''Unrolled Single  Precision''', force_c=True, output_parser=output_parser, shared_args=['-DSP'])

  def test_zzz_java_nbody(self): # tests xmlvm compiled java, including bitcasts of doubles, i64 math, etc.
    if CORE_BENCHMARKS: return
    args = [path_from_root('tests', 'nbody-java', x) for x in os.listdir(path_from_root('tests', 'nbody-java')) if x.endswith('.c')] + \
           ['-I' + path_from_root('tests', 'nbody-java')]
    self.do_benchmark('nbody_java', '', '''Time(s)''',
                      force_c=True, emcc_args=args + ['-s', 'PRECISE_I64_MATH=1', '--llvm-lto', '2'], native_args=args + ['-lgc', '-std=c99', '-target', 'x86_64-pc-linux-gnu', '-lm'])

  def lua(self, benchmark, expected, output_parser=None, args_processor=None):
    shutil.copyfile(path_from_root('tests', 'lua', benchmark + '.lua'), benchmark + '.lua')
    #shutil.copyfile(path_from_root('tests', 'lua', 'binarytrees.lua'), 'binarytrees.lua')
    #shutil.copyfile(path_from_root('tests', 'lua', 'scimark.lua'), 'scimark.lua')
    emcc_args = self.get_library('lua', [os.path.join('src', 'lua'), os.path.join('src', 'liblua.a')], make=['make', 'generic'], configure=None) + \
                ['--embed-file', benchmark + '.lua']
                #['--embed-file', 'binarytrees.lua', '--embed-file', 'scimark.lua'] + ['--minify', '0']
    shutil.copyfile(emcc_args[0], emcc_args[0] + '.bc')
    emcc_args[0] += '.bc'
    native_args = self.get_library('lua_native', [os.path.join('src', 'lua'), os.path.join('src', 'liblua.a')], make=['make', 'generic'], configure=None, native=True)

    self.do_benchmark('lua_' + benchmark, '', expected,
                      force_c=True, args=[benchmark + '.lua', DEFAULT_ARG], emcc_args=emcc_args, native_args=native_args, native_exec=os.path.join('building', 'lua_native', 'src', 'lua'),
                      output_parser=output_parser, args_processor=args_processor)

  def test_zzz_lua_scimark(self):
    def output_parser(output):
      return 100.0/float(re.search('\nSciMark +([\d\.]+) ', output).group(1))

    self.lua('scimark', '[small problem sizes]', output_parser=output_parser)

  def test_zzz_lua_binarytrees(self):
    # js version: ['binarytrees.lua', {0: 0, 1: 9.5, 2: 11.99, 3: 12.85, 4: 14.72, 5: 15.82}[arguments[0]]]
    self.lua('binarytrees', 'long lived tree of depth')

  def test_zzz_zlib(self):
    src = open(path_from_root('tests', 'zlib', 'benchmark.c'), 'r').read()
    def lib_builder(name, native, env_init):
      return self.get_library('zlib', os.path.join('libz.a'), make_args=['libz.a'], native=native, cache_name_extra=name, env_init=env_init)
    self.do_benchmark('zlib', src, '''ok.''',
                      force_c=True, shared_args=['-I' + path_from_root('tests', 'zlib')], lib_builder=lib_builder)

  def test_zzz_box2d(self): # Called thus so it runs late in the alphabetical cycle... it is long
    src = open(path_from_root('tests', 'box2d', 'Benchmark.cpp'), 'r').read()
    def lib_builder(name, native, env_init):
      return self.get_library('box2d', [os.path.join('box2d.a')], configure=None, native=native, cache_name_extra=name, env_init=env_init)
    self.do_benchmark('box2d', src, 'frame averages', shared_args=['-I' + path_from_root('tests', 'box2d')], lib_builder=lib_builder)

  def test_zzz_bullet(self): # Called thus so it runs late in the alphabetical cycle... it is long
    src = open(path_from_root('tests', 'bullet', 'Demos', 'Benchmarks', 'BenchmarkDemo.cpp'), 'r').read() + \
          open(path_from_root('tests', 'bullet', 'Demos', 'Benchmarks', 'main.cpp'), 'r').read()

    def lib_builder(name, native, env_init):
      return self.get_library('bullet', [os.path.join('src', '.libs', 'libBulletDynamics.a'),
                                         os.path.join('src', '.libs', 'libBulletCollision.a'),
                                         os.path.join('src', '.libs', 'libLinearMath.a')],
                              configure_args=['--disable-demos','--disable-dependency-tracking'], native=native, cache_name_extra=name, env_init=env_init)

    emcc_args = ['-s', 'DEAD_FUNCTIONS=["__ZSt9terminatev"]']

    self.do_benchmark('bullet', src, '\nok.\n', emcc_args=emcc_args, shared_args=['-I' + path_from_root('tests', 'bullet', 'src'),
                                '-I' + path_from_root('tests', 'bullet', 'Demos', 'Benchmarks')], lib_builder=lib_builder)

