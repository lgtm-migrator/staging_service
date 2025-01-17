import json
import logging
import os
import shutil
import sys
from collections import defaultdict
from urllib.parse import parse_qs
from pathlib import Path as PathPy

import aiohttp_cors
from aiohttp import web

from .app_error_formatter import format_import_spec_errors
from .AutoDetectUtils import AutoDetectUtils
from .JGIMetadata import read_metadata_for
from .auth2Client import KBaseAuth2
from .globus import assert_globusid_exists, is_globusid
from .metadata import some_metadata, dir_info, add_upa, similar
from .utils import Path, run_command, AclManager
from .import_specifications.file_parser import (
    ErrorType,
    FileTypeResolution,
    parse_import_specifications,
)
from .import_specifications.individual_parsers import parse_csv, parse_tsv, parse_excel
from .import_specifications.file_writers import (
    write_csv,
    write_tsv,
    write_excel,
    ImportSpecWriteException,
)
from .autodetect.Mappings import CSV, TSV, EXCEL


logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
routes = web.RouteTableDef()
VERSION = "1.3.5"

_DATATYPE_MAPPINGS = None

_APP_JSON = "application/json"

_IMPSPEC_FILE_TO_PARSER = {
    CSV: parse_csv,
    TSV: parse_tsv,
    EXCEL: parse_excel,
}

_IMPSPEC_FILE_TO_WRITER = {
    CSV: write_csv,
    TSV: write_tsv,
    EXCEL: write_excel,
}


@routes.get("/importer_filetypes/")
async def importer_filetypes(request: web.Request) -> web.json_response:
    """
    Returns the file types for the configured datatypes. The returned JSON contains two keys:
    * datatype_to_filetype, which maps import datatypes (like gff_genome) to their accepted
      filetypes (like [FASTA, GFF])
    * filetype_to_extensions, which maps file types (e.g. FASTA) to their extensions (e.g.
      *.fa, *.fasta, *.fa.gz, etc.)

    This information is currently static over the life of the server.
    """
    return web.json_response(data=_DATATYPE_MAPPINGS)


@routes.get("/importer_mappings/{query:.*}")
async def importer_mappings(request: web.Request) -> web.json_response:
    """
    Return a dictionary with two lists: apps and mappings
    Apps are a list of importers
    Mappings are a list of mapping between passed in files, and available apps
    :param request: contains a list of files e.g. ['file1.txt','file2.fasta']
    """

    file_list = parse_qs(request.query_string).get("file_list", [])
    if len(file_list) == 0:
        raise web.HTTPBadRequest(
            text=f"must provide file_list field. Your provided qs: {request.query_string}",
            )

    mappings = AutoDetectUtils.get_mappings(file_list)
    return web.json_response(data=mappings)


def _file_type_resolver(path: PathPy) -> FileTypeResolution:
    fi = AutoDetectUtils.get_mappings([str(path)])["fileinfo"][0]
    # Here we assume that the first entry in the file_ext_type field is the entry
    # we want. Presumably secondary entries are less general.
    ftype = fi['file_ext_type'][0] if fi['file_ext_type'] else None
    if ftype in _IMPSPEC_FILE_TO_PARSER:
        return FileTypeResolution(parser=_IMPSPEC_FILE_TO_PARSER[ftype])
    else:
        ext = fi['suffix'] if fi['suffix'] else path.suffix[1:] if path.suffix else path.name
        return FileTypeResolution(unsupported_type=ext)


@routes.get("/bulk_specification/{query:.*}")
async def bulk_specification(request: web.Request) -> web.json_response:
    """
    Takes a `files` query parameter with a list of comma separated import specification file paths.
    Returns the contents of those files parsed into a list of dictionaries, mapped from the data
    type, in the `types` key.

    :param request: contains a comma separated list of files, e.g. folder1/file1.txt,file2.txt
    """
    username = await authorize_request(request)
    files = parse_qs(request.query_string).get("files", [])
    files = files[0].split(",") if files else []
    files = [f.strip() for f in files if f.strip()]
    paths = {}
    for f in files:
        p = Path.validate_path(username, f)
        paths[PathPy(p.full_path)] = PathPy(p.user_path)
    # list(dict) returns a list of the dict keys in insertion order (py3.7+)
    res = parse_import_specifications(
        tuple(list(paths)),
        _file_type_resolver,
        lambda e: logging.error("Unexpected error while parsing import specs", exc_info=e))
    if res.results:
        types = {dt: result.result for dt, result in res.results.items()}
        files = {dt: {"file": str(paths[result.source.file]), "tab": result.source.tab}
            for dt, result in res.results.items()}
        return web.json_response({"types": types, "files": files})
    errtypes = {e.error for e in res.errors}
    errtext = json.dumps({"errors": format_import_spec_errors(res.errors, paths)})
    if errtypes - {ErrorType.OTHER, ErrorType.FILE_NOT_FOUND}:
        return web.HTTPBadRequest(text=errtext, content_type=_APP_JSON)
    if errtypes - {ErrorType.OTHER}:
        return web.HTTPNotFound(text=errtext, content_type=_APP_JSON)
    # I don't think there's a good way to test this codepath
    return web.HTTPInternalServerError(text=errtext, content_type=_APP_JSON)


@routes.post("/write_bulk_specification/")
async def write_bulk_specification(request: web.Request) -> web.json_response:
    """
    Write a bulk specification template to the user's staging area.

    :param request: Expectes a JSON body as a mapping with the following keys:
        output_directory - the location where the templates should be written.
        output_file_type - one of CSV, TSV, or EXCEL. Specifies the template format.
        types - specifies the contents of the templates. This is a dictionary of data types as
            strings to the specifications for the data type. Each specification has two required
            keys:
            * `order_and_display`: this is a list of lists. Each inner list has two elements:
                * The parameter ID of a parameter. This is typically the `id` field from the
                    KBase app `spec.json` file.
                * The display name of the parameter. This is typically the `ui-name` field from the
                    KBase app `display.yaml` file.
                The order of the inner lists in the outer list defines the order of the columns
                in the resulting import specification files.
            * `data`: this is a list of str->str or number dicts. The keys of the dicts are the
                parameter IDs as described above, while the values are the values of the
                parameters. Each dict must have exactly the same keys as the `order_and_display`
                structure. Each entry in the list corresponds to a row in the resulting import
                specification, and the order of the list defines the order of the rows.
            Leave the `data` list empty to write an empty template.

    :returns: A JSON mapping with the output_file_type key identical to the above, and a mapping
        of the data types in the input "types" field to the file created for that type.
    """
    username = await authorize_request(request)
    if request.content_type != _APP_JSON:
        # There should be a way to get aiohttp to handle this but I can't find it
        return _createJSONErrorResponse(
            f"Required content-type is {_APP_JSON}",
            error_class=web.HTTPUnsupportedMediaType)
    if not request.content_length:
        return _createJSONErrorResponse(
            "The content-length header is required and must be > 0",
            error_class=web.HTTPLengthRequired)
    # No need to check the max content length; the server already does that. See tests
    data = await request.json()
    if type(data) != dict:
        return _createJSONErrorResponse("The top level JSON element must be a mapping")
    folder = data.get('output_directory')
    type_ = data.get('output_file_type')
    if type(folder) != str:
        return _createJSONErrorResponse("output_directory is required and must be a string")
    writer = _IMPSPEC_FILE_TO_WRITER.get(type_)
    if not writer:
        return _createJSONErrorResponse(f"Invalid output_file_type: {type_}")
    folder = Path.validate_path(username, folder)
    os.makedirs(folder.full_path, exist_ok=True)
    try:
        files = writer(PathPy(folder.full_path), data.get("types"))
    except ImportSpecWriteException as e:
        return _createJSONErrorResponse(e.args[0])
    new_files = {ty: str(PathPy(folder.user_path) / files[ty]) for ty in files}
    return web.json_response({"output_file_type": type_, "files_created": new_files})


def _createJSONErrorResponse(error_text: str, error_class=web.HTTPBadRequest):
    err = json.dumps({"error": error_text})
    return error_class(text=err, content_type=_APP_JSON)


@routes.get("/add-acl-concierge")
async def add_acl_concierge(request: web.Request):
    username = await authorize_request(request)
    user_dir = Path.validate_path(username).full_path
    concierge_path = f"{Path._CONCIERGE_PATH}/{username}/"
    aclm = AclManager()
    result = aclm.add_acl_concierge(
        shared_directory=user_dir, concierge_path=concierge_path
    )
    result[
        "msg"
    ] = f"Requesting Globus Perms for the following globus dir: {concierge_path}"
    result[
        "link"
    ] = f"https://app.globus.org/file-manager?destination_id={aclm.endpoint_id}&destination_path={concierge_path}"
    return web.json_response(result)


@routes.get("/add-acl")
async def add_acl(request: web.Request):
    username = await authorize_request(request)
    user_dir = Path.validate_path(username).full_path
    result = AclManager().add_acl(user_dir)
    return web.json_response(result)


@routes.get("/remove-acl")
async def remove_acl(request: web.Request):
    username = await authorize_request(request)
    user_dir = Path.validate_path(username).full_path
    result = AclManager().remove_acl(user_dir)
    return web.json_response(result)


@routes.get("/test-service")
async def test_service(request: web.Request):
    return web.Response(text="staging service version: {}".format(VERSION))


@routes.get("/test-auth")
async def test_auth(request: web.Request):
    username = await authorize_request(request)
    return web.Response(text="I'm authenticated as {}".format(username))


@routes.get("/file-lifetime")
async def file_lifetime(parameter_list):
    return web.Response(text=os.environ["FILE_LIFETIME"])


@routes.get("/existence/{query:.*}")
async def file_exists(request: web.Request):
    username = await authorize_request(request)
    query = request.match_info["query"]
    user_dir = Path.validate_path(username)
    try:
        show_hidden = request.query["showHidden"]
        if "true" == show_hidden or "True" == show_hidden:
            show_hidden = True
        else:
            show_hidden = False
    except KeyError as no_query:
        show_hidden = False
    # this scans the entire directory recursively just to see if one file exists... why?
    results = await dir_info(user_dir, show_hidden, query)
    filtered_results = [result for result in results if result["name"] == query]
    if filtered_results:
        exists = True
        is_folder = [file_json["isFolder"] for file_json in filtered_results]
        isFolder = all(is_folder)
    else:
        exists = False
        isFolder = False
    return web.json_response({"exists": exists, "isFolder": isFolder})


@routes.get("/list/{path:.*}")
@routes.get("/list")
async def list_files(request: web.Request):
    """
    lists the contents of a directory and some details about them
    """
    username = await authorize_request(request)
    path = Path.validate_path(username, request.match_info.get("path", ""))
    if not os.path.exists(path.full_path):
        raise web.HTTPNotFound(
            text="path {path} does not exist".format(path=path.user_path)
        )
    elif os.path.isfile(path.full_path):
        raise web.HTTPBadRequest(
            text="{path} is a file not a directory".format(path=path.full_path)
        )
    try:
        show_hidden = request.query["showHidden"]
        if "true" == show_hidden or "True" == show_hidden:
            show_hidden = True
        else:
            show_hidden = False
    except KeyError as no_query:
        show_hidden = False
    data = await dir_info(path, show_hidden, recurse=True)
    return web.json_response(data)


@routes.get("/download/{path:.*}")
async def download_files(request: web.Request):
    """
    download a file
    """
    username = await authorize_request(request)
    path = Path.validate_path(username, request.match_info.get("path", ""))
    if not os.path.exists(path.full_path):
        raise web.HTTPNotFound(
            text="path {path} does not exist".format(path=path.user_path)
        )
    elif not os.path.isfile(path.full_path):
        raise web.HTTPBadRequest(
            text="{path} is a directory not a file".format(path=path.full_path)
        )
    # hard coding the mime type to force download
    return web.FileResponse(
        path.full_path, headers={"content-type": "application/octet-stream"}
    )


@routes.get("/similar/{path:.+}")
async def similar_files(request: web.Request):
    """
    lists similar file path for given file
    """
    username = await authorize_request(request)
    path = Path.validate_path(username, request.match_info["path"])
    if not os.path.exists(path.full_path):
        raise web.HTTPNotFound(
            text="path {path} does not exist".format(path=path.user_path)
        )
    elif os.path.isdir(path.full_path):
        raise web.HTTPBadRequest(
            text="{path} is a directory not a file".format(path=path.full_path)
        )

    root = Path.validate_path(username, "")
    files = await dir_info(root, show_hidden=False, recurse=True)

    similar_files = list()
    similarity_cut_off = 0.75  # adjust this cut off if necessary
    for file in files:
        if (not file.get("isFolder")) and (path.user_path != file.get("path")):
            similar_match = await similar(
                os.path.basename(path.user_path), file.get("name"), similarity_cut_off
            )
            if similar_match:
                similar_files.append(file)

    return web.json_response(similar_files)


@routes.get("/search/{query:.*}")
async def search(request: web.Request):
    """
    returns all files and folders matching the search query ordered by modified date
    """
    username = await authorize_request(request)
    query = request.match_info["query"]
    user_dir = Path.validate_path(username)
    try:
        show_hidden = request.query["showHidden"]
        if "true" == show_hidden or "True" == show_hidden:
            show_hidden = True
        else:
            show_hidden = False
    except KeyError as no_query:
        show_hidden = False
    results = await dir_info(user_dir, show_hidden, query)
    results.sort(key=lambda x: x["mtime"], reverse=True)
    return web.json_response(results)


@routes.get("/metadata/{path:.*}")
async def get_metadata(request: web.Request):
    """
    creates a metadate file for the file requested and returns its json contents
    if it's a folder it returns stat data about the folder
    """
    username = await authorize_request(request)
    path = Path.validate_path(username, request.match_info["path"])
    if not os.path.exists(path.full_path):
        raise web.HTTPNotFound(
            text="path {path} does not exist".format(path=path.user_path)
        )
    return web.json_response(await some_metadata(path))


@routes.get("/jgi-metadata/{path:.*}")
async def get_jgi_metadata(request: web.Request):
    """
    returns jgi metadata if associated with a file
    """
    username = await authorize_request(request)
    path = Path.validate_path(username, request.match_info["path"])
    return web.json_response(await read_metadata_for(path))


@routes.post("/upload")
async def upload_files_chunked(request: web.Request):
    """
    uploads a file into the staging area
    """
    username = await authorize_request(request)

    if not request.has_body:
        raise web.HTTPBadRequest(text="must provide destPath and uploads in body")

    reader = await request.multipart()
    counter = 0
    user_file = None
    destPath = None
    while (
        counter < 100
    ):  # TODO this is arbitrary to keep an attacker from creating infinite loop
        # This loop handles the null parts that come in inbetween destpath and file
        part = await reader.next()

        if part.name == "destPath":
            destPath = await part.text()
        elif part.name == "uploads":
            user_file = part
            break
        else:
            counter += 1

    if not (user_file and destPath):
        raise web.HTTPBadRequest(text="must provide destPath and uploads in body")

    filename: str = user_file.filename
    if filename.lstrip() != filename:
        raise web.HTTPForbidden(  # forbidden isn't really the right code, should be 400
            text="cannot upload file with name beginning with space"
        )
    if "," in filename:
        raise web.HTTPForbidden(  # for consistency we use 403 again
            text="cannot upload file with ',' in name"
        )
    # may want to make this configurable if we ever decide to add a hidden files toggle to
    # the staging area UI
    if filename.startswith("."):
        raise web.HTTPForbidden(  # for consistency we use 403 again
            text="cannot upload file with name beginning with '.'"
        )

    size = 0
    destPath = os.path.join(destPath, filename)
    path = Path.validate_path(username, destPath)
    os.makedirs(os.path.dirname(path.full_path), exist_ok=True)
    with open(path.full_path, "wb") as f:  # TODO should we handle partial file uploads?
        while True:
            chunk = await user_file.read_chunk()
            if not chunk:
                break
            size += len(chunk)
            f.write(chunk)

    if not os.path.exists(path.full_path):
        error_msg = "We are sorry but upload was interrupted. Please try again.".format(
            path=path.full_path
        )
        raise web.HTTPNotFound(text=error_msg)

    response = await some_metadata(
        path,
        desired_fields=["name", "path", "mtime", "size", "isFolder"],
        source="KBase upload",
    )
    return web.json_response([response])


@routes.post("/define-upa/{path:.+}")
async def define_UPA(request: web.Request):
    """
    creates an UPA as a field in the metadata file corresponding to the filepath given
    """
    username = await authorize_request(request)
    path = Path.validate_path(username, request.match_info["path"])
    if not os.path.exists(path.full_path or not os.path.isfile(path.full_path)):
        # TODO the security model here is to not care if someone wants to put in a false upa
        raise web.HTTPNotFound(
            text="no file found found on path {}".format(path.user_path)
        )
    if not request.has_body:
        raise web.HTTPBadRequest(text="must provide UPA field in body")
    body = await request.post()
    try:
        UPA = body["UPA"]
    except KeyError as wrong_key:
        raise web.HTTPBadRequest(text="must provide UPA field in body")
    await add_upa(path, UPA)
    return web.Response(
        text="succesfully updated UPA {UPA} for file {path}".format(
            UPA=UPA, path=path.user_path
        )
    )


@routes.delete("/delete/{path:.+}")
async def delete(request: web.Request):
    """
    allows deletion of both directories and files
    """
    username = await authorize_request(request)
    path = Path.validate_path(username, request.match_info["path"])
    # make sure directory isn't home
    if path.user_path == username:
        raise web.HTTPForbidden(text="cannot delete home directory")
    if is_globusid(path, username):
        raise web.HTTPForbidden(text="cannot delete protected file")
    if os.path.isfile(path.full_path):
        os.remove(path.full_path)
        if os.path.exists(path.metadata_path):
            os.remove(path.metadata_path)
    elif os.path.isdir(path.full_path):
        shutil.rmtree(path.full_path)
        if os.path.exists(path.metadata_path):
            shutil.rmtree(path.metadata_path)
    else:
        raise web.HTTPNotFound(
            text="could not delete {path}".format(path=path.user_path)
        )
    return web.Response(text="successfully deleted {path}".format(path=path.user_path))


@routes.patch("/mv/{path:.+}")
async def rename(request: web.Request):
    username = await authorize_request(request)
    path = Path.validate_path(username, request.match_info["path"])

    # make sure directory isn't home
    if path.user_path == username:
        raise web.HTTPForbidden(text="cannot rename or move home directory")
    if is_globusid(path, username):
        raise web.HTTPForbidden(text="cannot rename or move protected file")
    if not request.has_body:
        raise web.HTTPBadRequest(text="must provide newPath field in body")
    body = await request.post()
    try:
        new_path = body["newPath"]
    except KeyError as wrong_key:
        raise web.HTTPBadRequest(text="must provide newPath field in body")
    new_path = Path.validate_path(username, new_path)
    if os.path.exists(path.full_path):
        if not os.path.exists(new_path.full_path):
            shutil.move(path.full_path, new_path.full_path)
            if os.path.exists(path.metadata_path):
                shutil.move(path.metadata_path, new_path.metadata_path)
        else:
            raise web.HTTPConflict(
                text="{new_path} allready exists".format(new_path=new_path.user_path)
            )
    else:
        raise web.HTTPNotFound(text="{path} not found".format(path=path.user_path))
    return web.Response(
        text="successfully moved {path} to {new_path}".format(
            path=path.user_path, new_path=new_path.user_path
        )
    )


@routes.patch("/decompress/{path:.+}")
async def decompress(request: web.Request):
    username = await authorize_request(request)
    path = Path.validate_path(username, request.match_info["path"])
    # make sure the file can be decompressed
    filename, file_extension = os.path.splitext(path.full_path)
    filename, upper_file_extension = os.path.splitext(filename)
    # TODO behavior when the unzip would overwrite something, what does it do, what should it do
    # 1 if we just don't let it do this its important to provide the rename feature,
    # 2 could try again after doign an automatic rename scheme (add nubmers to end)
    # 3 just overwrite and force
    destination = os.path.dirname(path.full_path)
    if (
        upper_file_extension == ".tar" and file_extension == ".gz"
    ) or file_extension == ".tgz":
        await run_command("tar", "xzf", path.full_path, "-C", destination)
    elif upper_file_extension == ".tar" and (
        file_extension == ".bz" or file_extension == ".bz2"
    ):
        await run_command("tar", "xjf", path.full_path, "-C", destination)
    elif file_extension == ".zip" or file_extension == ".ZIP":
        await run_command("unzip", path.full_path, "-d", destination)
    elif file_extension == ".tar":
        await run_command("tar", "xf", path.full_path, "-C", destination)
    elif file_extension == ".gz":
        await run_command("gzip", "-d", path.full_path)
    elif file_extension == ".bz2" or file_extension == "bzip2":
        await run_command("bzip2", "-d", path.full_path)
    else:
        raise web.HTTPBadRequest(
            text="cannot decompress a {ext} file".format(ext=file_extension)
        )
    return web.Response(text="succesfully decompressed " + path.user_path)


async def authorize_request(request):
    """
    Authenticate a token from kbase_session in cookies or Authorization header and return the
     username
    """
    if request.headers.get("Authorization"):
        token = request.headers.get("Authorization")
    elif request.cookies.get("kbase_session"):
        token = request.cookies.get("kbase_session")
    else:
        # this is a hack for prod because kbase_session won't get shared with the kbase.us domain
        token = request.cookies.get("kbase_session_backup")
    username = await auth_client.get_user(token)
    await assert_globusid_exists(username, token)
    return username


def inject_config_dependencies(config):
    """
    # TODO this is pretty hacky dependency injection
    # potentially some type of code restructure would allow this without a bunch of globals
    # This overwrites the PATH class and the AutoDetectUtils Class
    :param config: The staging service main config
    """

    DATA_DIR = config["staging_service"]["DATA_DIR"]
    META_DIR = config["staging_service"]["META_DIR"]
    CONCIERGE_PATH = config["staging_service"]["CONCIERGE_PATH"]
    FILE_EXTENSION_MAPPINGS = config["staging_service"]["FILE_EXTENSION_MAPPINGS"]

    if DATA_DIR.startswith("."):
        DATA_DIR = os.path.normpath(os.path.join(os.getcwd(), DATA_DIR))
    if META_DIR.startswith("."):
        META_DIR = os.path.normpath(os.path.join(os.getcwd(), META_DIR))
    if CONCIERGE_PATH.startswith("."):
        CONCIERGE_PATH = os.path.normpath(os.path.join(os.getcwd(), CONCIERGE_PATH))
    if FILE_EXTENSION_MAPPINGS.startswith("."):
        FILE_EXTENSION_MAPPINGS = os.path.normpath(
            os.path.join(os.getcwd(), FILE_EXTENSION_MAPPINGS)
        )

    Path._DATA_DIR = DATA_DIR
    Path._META_DIR = META_DIR
    Path._CONCIERGE_PATH = CONCIERGE_PATH

    if Path._DATA_DIR is None:
        raise Exception("Please provide DATA_DIR in the config file ")

    if Path._META_DIR is None:
        raise Exception("Please provide META_DIR in the config file ")

    if Path._CONCIERGE_PATH is None:
        raise Exception("Please provide CONCIERGE_PATH in the config file ")

    if FILE_EXTENSION_MAPPINGS is None:
        raise Exception("Please provide FILE_EXTENSION_MAPPINGS in the config file ")
    with open(FILE_EXTENSION_MAPPINGS) as f:
        AutoDetectUtils._MAPPINGS = json.load(f)
        datatypes = defaultdict(set)
        extensions = defaultdict(set)
        for fileext, val in AutoDetectUtils._MAPPINGS["types"].items():
            # if we start using the file ext type array for anything else this might need changes
            filetype = val["file_ext_type"][0]
            extensions[filetype].add(fileext)
            for m in val['mappings']:
                datatypes[m['id']].add(filetype)
        global _DATATYPE_MAPPINGS
        _DATATYPE_MAPPINGS = {
            "datatype_to_filetype": {k: sorted(datatypes[k]) for k in datatypes},
            "filetype_to_extensions": {k: sorted(extensions[k]) for k in extensions},
        }


def app_factory(config):
    app = web.Application(middlewares=[web.normalize_path_middleware()])
    app.router.add_routes(routes)
    cors = aiohttp_cors.setup(
        app,
        defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True, expose_headers="*", allow_headers="*"
            )
        },
    )
    # Configure CORS on all routes.
    for route in list(app.router.routes()):
        cors.add(route)

    inject_config_dependencies(config)

    global auth_client
    auth_client = KBaseAuth2(config["staging_service"]["AUTH_URL"])
    return app
