/* Python extension to interact with CRASH
   
  Time-stamp: <11/02/15 11:32:42 alexs>

  Copyright (C) 2006-2007 Alex Sidorenko <asid@hp.com>
  Copyright (C) 2006-2007 Hewlett-Packard Co., All rights reserved.
 
  This program is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 2 of the License, or
  (at your option) any later version.
 
  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.
*/

#include <Python.h>
#include <compile.h>        /* for PyCodeObject typedef on older releases */
#include <eval.h>           /* for PyEval_EvalCode om older released */

#include <unistd.h>
#include <stdlib.h>
#include <sys/times.h>


#include "defs.h"    /* From the crash source top-level directory */

int debug = 0;

static char crash_version[] = "@(#)pycrash 0.6.4";

static char *ext_filename = NULL;
#define BUFLEN 1024


/* Initialize the crashmodule stuff */
void initcrash(const char *) ;

static int run_fromzip(const char *progname, const char* zipfile);

/* This function is called when we do sys.exit(n). The standard Py_Exit is
   defines in Python sourcefile Modules/pythonrun.c and it does Py_Finalize
   and exit(). This will destroy the intepreter and terminate crash.
   We don't want our extensions to terminate crash, so we link-in our own
   version of this function. This works on IA32 but I am not sure about other
   architectures
*/


void
Py_Exit(int sts) {
  if (sts)
    printf("sys.exit(%d)\n", sts);
}


// The next pair of functions makes it possible to run some tasks
// just before we start executing 'epython ...' and before we return
// to 'crash' prompt

// Entering
static void
call_sys_enterepython(void)
{
        PyObject *enterfunc = PySys_GetObject("enterepython");

        if (enterfunc) {
                PyObject *res;
                Py_INCREF(enterfunc);
                //PySys_SetObject("enterepython", (PyObject *)NULL);
                res = PyEval_CallObject(enterfunc, (PyObject *)NULL);
                if (res == NULL) {
                        if (!PyErr_ExceptionMatches(PyExc_SystemExit)) {
                                PySys_WriteStderr("Error in sys.enterepython:\n");
                        }
                        PyErr_Print();
                }
                Py_DECREF(enterfunc);
        }

        if (Py_FlushLine())
                PyErr_Clear();
}

// Exiting
static void
call_sys_exitepython(void)
{
        PyObject *exitfunc = PySys_GetObject("exitepython");

        if (exitfunc) {
                PyObject *res;
                Py_INCREF(exitfunc);
                //PySys_SetObject("exitepython", (PyObject *)NULL);
                res = PyEval_CallObject(exitfunc, (PyObject *)NULL);
                if (res == NULL) {
                        if (!PyErr_ExceptionMatches(PyExc_SystemExit)) {
                                PySys_WriteStderr("Error in sys.exitepython:\n");
                        }
                        PyErr_Print();
                }
                Py_DECREF(exitfunc);
        }

        if (Py_FlushLine())
                PyErr_Clear();
}

void cmd_epython();     /* Declare the commands and their help data. */
char *help_epython[];

static struct command_table_entry command_table[] = {
        "epython", cmd_epython, help_epython, 0,           /* One or more commands, */
        NULL,                                     /* terminated by NULL, */
};

struct extension_table *epython_curext;

static int py_fclose(FILE *fp) {
  return 0;
}

/* There is a problem when unloading the extension built with Python
   shared library. In this case we load other .so files as needed.
   As a result, the reference count of or .so does not go to zero and
   when you load again, _init is not called. This is true even for
   __attribute__ mechanism. But everything's OK for ZIPped version
*/

/* Old-style constructrs/destructors for dlopen. */
void _init(void)  {
//void __attribute__((constructor)) n_init(void) {
  PyObject *syspath, *sysm;
  char buffer[BUFLEN];
  PyObject *s;

  struct command_table_entry *ct_copy;
    
  /*
    WARNING:
    dlopen() manpage says that _init() is not very reliable and can be called
    twice in some cases.
  */
  if (getenv("PYKDUMPDEBUG"))
    debug = atoi(getenv("PYKDUMPDEBUG"));
  if (debug)
    printf("Running epython_init\n");
  
  /* Before doing anything else, check whether the versions of crash 
    used for build and the currently running version are compatible:
    build_crash_version vs build_version
   */
  
  extern const char *build_crash_version;
  if (build_crash_version[0] != build_version[0]) {
    fprintf(stderr, "\nYou need to use mpykdump.so matching the major"
	" crash version\n");
    fprintf(stderr, "crash used for build: %s, currently running: %s\n",
	 build_crash_version, build_version);
    fprintf(stderr, "Cannot continue, exiting\n\n");
    exit(1);
  }
  
  /* crash-5 is a moving target and we usually need to rebuild the
     module with each new release. Output a warning
  */

  if (build_version[0] == '5' && strcmp(build_crash_version, build_version)) {
    /* Turn off scrolling */
    fprintf(fp, "PyKdump built for crash %s, currently running: %s\n",
	   build_crash_version, build_version);
    fprintf(fp, "If something does not work, use the matching versions\n");
  }
      
  

  ext_filename = malloc(strlen(pc->curext->filename)+1);
  strcpy(ext_filename,  pc->curext->filename);
  if (debug)
    printf("extname=%s\n", ext_filename);

  // Store our extension table for registering subcommands
  epython_curext = pc->curext;

  if (!Py_IsInitialized()) {
#if defined(STATICBUILD)
    Py_NoSiteFlag = 1;
    Py_FrozenFlag = 1;
    Py_IgnoreEnvironmentFlag = 1;
    Py_SetPythonHome("");
#endif
    if (debug)
      fprintf(fp, "     *** Initializing Embedded Python %s ***\n",
	      crash_version+12);
    Py_Initialize();
    PyEval_InitThreads();
    initcrash(crash_version+12);
    //sysm = PyImport_ImportModule("sys");
    // For static builds, reset sys.path from scratch
#if defined(STATICBUILD)
    PySys_SetPath("");
    syspath = PySys_GetObject("path");
    s = PyString_FromString(ext_filename);
    PyList_Append(syspath, s);
    //PyList_SetItem(syspath, 0, s);
    Py_DECREF(s);
    strcpy(buffer, ext_filename);
    strcat(buffer, "/pylib");
    s = PyString_FromString(buffer);
    PyList_Append(syspath, s);
    //Py_DECREF(syspath);
    Py_DECREF(s);
#endif
  } else {
    if (debug)
      printf("Trying to Py_Initialize() twice\n");
  }

  /* Make a copy of the initial command table on heap, so we'll be able to
     modify it if needed
  */

  ct_copy = (struct command_table_entry *) malloc(sizeof(command_table));
  if (!ct_copy) {
    printf("Cannot allocate ct_copy\n");
    exit(1);
  }
  memcpy(ct_copy, command_table, sizeof(command_table));
  register_extension(ct_copy);
  
  if (debug) {
    printf("Epython extension registered\n");
    PyRun_SimpleString("import sys; print sys.path");
  }

  // Run the initialization Python script if it is available
  {
    char *argv[]= {"PyKdumpInit", NULL};
    epython_execute_prog(1, argv, 1);
  }
  
  return;
}
 
void _fini(void) {
  //void __attribute__((destructor)) n_fini(void) {
  struct command_table_entry *ce;

  if (!debug)
    return;
    
  printf("Unloading epython\n");
  free(ext_filename);
  ext_filename = NULL;
  Py_Finalize();

  // Free name and help pointers for added entries
  for (ce = epython_curext->command_table, ce++; ce->name; ce++) {
    printf("freeing ce->name and ce->help_data for %s\n", ce->name);
    free(ce->name);
    if (ce->help_data)
      free(ce->help_data);
  }

  free(epython_curext->command_table);
}



/*
  Try to run the program from internal ZIP (should be in progs/).
 */

static int
run_fromzip(const char *progname, const char *zipfilename) {
  PyObject *main, *m, *importer;
  PyCodeObject *code;
  PyObject *d, *v;
  PyObject *ZipImportError;
  m = PyImport_ImportModule("zipimport");
  if (!m) {
    printf("Cannot import <zipimport> module\n");
    return 0;
  }
  importer = PyObject_CallMethod(m, "zipimporter", "s", zipfilename);
  Py_DECREF(m);

  code = (PyCodeObject *) PyObject_CallMethod(importer, "get_code", "s",
					      progname);
  Py_DECREF(importer);
  if (!code) {
    printf("Cannot getcode for <%s>\n", progname);
    PyErr_Clear();
    return 0;
  }

  m = PyImport_AddModule("__main__");
  if (m == NULL) {
    PyErr_Print();
    return 0;
  }

  d = PyModule_GetDict(m);
  v =  PyString_FromString(progname);
  PyDict_SetItemString(d, "__file__", v);
  Py_DECREF(v);

  /* Execute code in __main__ context */
  if (debug)
    printf("Executing code from ZIP\n\n");
  v = PyEval_EvalCode(code, d, d);

  Py_DECREF(code);
 
  if (v == NULL) {
    // Even though we have been able to run the program, it has ended
    // raising an exception
    if (PyErr_ExceptionMatches(PyExc_IOError)) {
      // We don't want to print error messages for Broken pipe
      // as they occur when we use a scroller and press 'q'
      // before reaching the end of output
      if (errno != EPIPE)
	PyErr_Print();
      else
	PyErr_Clear();
    } else 
      PyErr_Print();
    return 1;
  }
  Py_DECREF(v);
  return 1;
  
}

/* 
 *  Arguments are passed to the command functions in the global args[argcnt]
 *  array.  See getopt(3) for info on dash arguments.  Check out defs.h and
 *  other crash commands for usage of the myriad of utility routines available
 *  to accomplish what your task.
 */



/* Search for our Python program:
	    1. Check whether we have it in the current directory
	    2. Check in the PATH
	    3. If filename does not have '.py' suffix, repeat (1-2)
	       after appending it to the specified name
*/

const char *path;
const char *find_pyprog(const char *prog) {
    char progpy[BUFSIZE];
    char buf2[BUFSIZE];
    static char buf1[BUFSIZE];
    char *tok;

    //If prognames start from '/', no need to search
    if (prog[0] == '/')
        return prog;

    if (path) {
        strcpy(buf2, ".:");
        strcat(buf2, path);
    } else
        strcpy(buf2, ".");

    tok = strtok(buf2, ":");
    while (tok) {
        sprintf(buf1, "%s/%s", tok, prog);
        if (debug > 2)
           printf("Checking %s\n", buf1);
        if (file_exists(buf1, NULL)) {
          if (debug > 1)
            printf("Found: %s\n",  buf1);
          return buf1;
        }
        sprintf(buf1, "%s/%s.py", tok, prog);
        if (debug > 2)
           printf("Checking %s\n", buf1);
        if (file_exists(buf1, NULL)) {
          if (debug > 2)
            printf("Found: %s\n",  buf1);
          return buf1;
        }
        tok = strtok(NULL, ":");
    }
    return NULL;
}

/*
  This subroutine executes a Python program in crash environment.
  We pass to it argc and argv.
  If 'quiet' is 1, we do not report errors and do not print stats.
  This is used for one-time initialiation script
*/

int
epython_execute_prog(int argc, char *argv[], int quiet) {
  FILE *scriptfp = NULL;
  PyObject *crashfp;
  PyObject *sysm;
  static long TICKSPS;
  const char *pypath;
  const char *prog;
  char buffer[BUFLEN];

  
  // Search in PYKDUMPPATH. If there is no '.py' suffix try to append it
  path = getenv("PYKDUMPPATH");
  if (argc < 1) {
    fprintf(fp, " You need to specify a program file\n");
    // No arguments passed
    return;
  }

  
  prog = find_pyprog(argv[0]);
  if (prog) {
    argv[0] = (char *) prog;		/* Is hopefully OK */
    scriptfp = fopen(prog, "r");
    /* No need to do anything if the file does not exist */
    if (scriptfp == NULL) {
      fprintf(fp, " Cannot open the file <%s>\n", prog);
      return;
    }
  }

  sysm = PyImport_ImportModule("sys");
  
  // Connect sys.stdout to fp
  crashfp = PyFile_FromFile(fp, "<crash fp>", "w", py_fclose);

  // We should add handling exceptions here to prevent 'crash' from exiting
  if (argc > 0) {
    /* PySys_SetArgv prepends to sys.path, don't forget to remove it later */
    PySys_SetArgv(argc, argv);
    PyModule_AddObject(sysm, "stdout", crashfp);

    /* The function will be available only on the 2nd and further invocations
     of epython as it is normally defined in API.py which is not loaded yet */
    if (!quiet)
      call_sys_enterepython();
    /* This is where we run the real user-provided script */

    if (scriptfp) {
      PyRun_SimpleFile(scriptfp, argv[0]);
      fclose(scriptfp);
    
    } else {
      /* Try to load code from ZIP */
      int rc = 0;
#if defined(STATICBUILD)
      if (quiet)
	strcpy(buffer, "");	/* Initprog is in top dir */
      else
	strcpy(buffer, "progs/");
      rc = run_fromzip(strncat(buffer, argv[0], BUFLEN - 60), ext_filename);
#endif
      if (!rc && !quiet)
	fprintf(fp, " Cannot find the program <%s>\n", argv[0]);
    }

    // Remove from sys.path the 1st element, inserted by PySys_SetArgv
    PySequence_DelItem(PySys_GetObject("path"), 0);

  } 
  // Run epython exitfuncs (if registered)
  if (!quiet)
    call_sys_exitepython();
  fflush(fp);
}

void
cmd_epython() {
  // We just strip 'epython' from argument list
  epython_execute_prog(argcnt - 1, args + 1, 0);
}

 
char *help_epython[] = {
        "epython",                        /* command name */
        "invokes embedded Python interpreter",   /* short description */
        "program.py arg ...",	/* argument synopsis, or " " if none */
 
        "  This command invokes embedded Python.",
        "\nEXAMPLE",
        "  Output help information for 'xportshow' tool:\n",
        "    crash> epython xportshow.py --help",
        NULL
};


