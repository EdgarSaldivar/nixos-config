{ lib, paths }:
let
  # These hashes are the recursive semantic identities of each complete app
  # table after removing only video/audio and parsing base_create_json. They
  # make process runners just as reviewable as containers without maintaining
  # a dangerous allowlist of selected fields.
  expectedHashes = {
    moonlight-profile-id = {
      "Wolf UI" = "7102850931832d621a7ad14ccedba12fd0742ae2c6e4f2e1ae6c2b6438a41154";
      "Test ball" = "25a509a849e052f0d34dfb90c5b604164c2d4ab2ecd8dae9e9b10df31403c159";
    };
    user = {
      Firefox = "7ef2f931c77c662e188131bdc5da66a28763efe63cdc433d79418b3401b1fef3";
      RetroArch = "086fb07a0b712088f704cdd6f663750629397bfa6dda6620838da7834b6c83b8";
      Steam = "10fbd17a442c91abbfe32d1636ebdda3d194c9b7132ee6b4bcca83d0e96aeebb";
      Pegasus = "71ac49162a4bad283065ed852d376e3c630b504db0de3018680232370d270351";
      Lutris = "ab3f3ed6f15ec0e64a892d19cec0a12eedaf76ea9b10bbb25852b529095df4e3";
      Prismlauncher = "7db14fa2870a5ce00334ba8b33acfcd06cc4bfc1107831e6a8e34bbc8c9b7db6";
      "Desktop (xfce)" = "1c4fd7588fd71a67ae5fb43c8d2c82aa082b1beced7e58b0b2206d5c75db786a";
      EmulationStation = "54e8a466c3d28a912cf439316cf66df495882fbe368e882abacb3ad890a66500";
      Kodi = "91648eb6d0aa67d21e708cd25692136c9eb4b831096fadd9d5984956fe4c88bb";
    };
    guest = {
      Steam = "74aac59bdaf2a815dc7cf65736db8f9aa90d53b7f0f2164df0c471abf1885a0b";
      "Desktop (xfce)" = "a4339512c13c296949192800239440e432c1dce3f4a6655bb8f0ca4696816ac1";
    };
  };
  multiplicities = {
    moonlight-profile-id = {
      "Wolf UI" = {
        min = 1;
        max = 1;
      };
      "Test ball" = {
        min = 0;
        max = 1;
      };
    };
    user = lib.mapAttrs (title: _: {
      min =
        if
          lib.elem title [
            "Steam"
            "Desktop (xfce)"
          ]
        then
          1
        else
          0;
      max = 1;
    }) expectedHashes.user;
    guest = lib.mapAttrs (_: _: {
      min = 1;
      max = 1;
    }) expectedHashes.guest;
  };
  expectedNames = {
    user = "Edgar";
    guest = "Guest";
  };
  count = predicate: values: builtins.length (builtins.filter predicate values);
  unique =
    values:
    lib.foldl' (seen: value: if lib.elem value seen then seen else seen ++ [ value ]) [ ] values;
  normalizeApp =
    app:
    let
      withoutOverrides = builtins.removeAttrs app [
        "video"
        "audio"
      ];
      runner = withoutOverrides.runner or { };
      normalizedRunner =
        if runner ? base_create_json then
          runner // { base_create_json = builtins.fromJSON runner.base_create_json; }
        else
          runner;
    in
    withoutOverrides // { runner = normalizedRunner; };
  appHash = app: builtins.hashString "sha256" (builtins.toJSON (normalizeApp app));
  splitMount = mount: lib.splitString ":" mount;
  mountSource = mount: builtins.head (splitMount mount);
  mountMode =
    mount:
    let
      pieces = splitMount mount;
    in
    if builtins.length pieces >= 3 then lib.last pieces else "rw";
  runnerMounts = app: (app.runner or { }).mounts or [ ];
  profileApps = profile: profile.apps or [ ];
  profileTitles = profile: map (app: app.title or null) (profileApps profile);
  ownedRoots =
    profileId:
    map (key: paths.${profileId}.${key}) [
      "steamapps"
      "nonsteam"
      "mods"
    ];
  sourceIsWithin = root: source: source == root || lib.hasPrefix "${root}/" source;
  validateUnchecked =
    config:
    let
      profiles = config.profiles or [ ];
      profileIds = map (profile: profile.id or null) profiles;
      profilesUnique = builtins.length profileIds == builtins.length (unique profileIds);
      reviewedIds = builtins.attrNames expectedHashes;
      idsExact = lib.sort builtins.lessThan profileIds == lib.sort builtins.lessThan reviewedIds;
      validateProfile =
        profile:
        let
          profileId = profile.id;
          apps = profileApps profile;
          titles = profileTitles profile;
          rules = multiplicities.${profileId};
          identityValid = lib.all (
            app:
            app ? title
            && builtins.hasAttr app.title expectedHashes.${profileId}
            && appHash app == expectedHashes.${profileId}.${app.title}
          ) apps;
          multiplicityValid = lib.all (
            title:
            let
              amount = count (candidate: candidate == title) titles;
            in
            amount >= rules.${title}.min && amount <= rules.${title}.max
          ) (builtins.attrNames rules);
          displayValid =
            !(builtins.hasAttr profileId expectedNames) || (profile.name or null) == expectedNames.${profileId};
          mounts = lib.concatMap runnerMounts apps;
          namedValid = lib.all (
            mount:
            let
              source = mountSource mount;
            in
            lib.hasPrefix "/" source || lib.elem source (paths.${profileId}.namedVolumes or [ ])
          ) mounts;
          foreignWriteValid =
            if
              !(lib.elem profileId [
                "user"
                "guest"
              ])
            then
              true
            else
              let
                other = if profileId == "user" then "guest" else "user";
              in
              lib.all (
                mount:
                let
                  source = mountSource mount;
                in
                mountMode mount != "rw" || !(lib.any (root: sourceIsWithin root source) (ownedRoots other))
              ) mounts;
        in
        builtins.length titles == builtins.length (unique titles)
        && identityValid
        && multiplicityValid
        && displayValid
        && namedValid
        && foreignWriteValid;
      namedUses = lib.concatMap (
        profile:
        map
          (mount: {
            profile = profile.id;
            source = mountSource mount;
          })
          (
            builtins.filter (mount: !(lib.hasPrefix "/" (mountSource mount))) (
              lib.concatMap runnerMounts (profileApps profile)
            )
          )
      ) profiles;
      namedUnshared = lib.all (
        use:
        let
          owners = unique (
            map (candidate: candidate.profile) (
              builtins.filter (candidate: candidate.source == use.source) namedUses
            )
          );
        in
        builtins.length owners == 1
      ) namedUses;
    in
    profilesUnique && idsExact && lib.all validateProfile profiles && namedUnshared;
in
config:
let
  attempted = builtins.tryEval (validateUnchecked config);
in
attempted.success && attempted.value
